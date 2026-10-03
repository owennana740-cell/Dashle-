"""Fournisseurs réels d'outils Dashle, côté serveur uniquement.

Les implémentations utilisent les contrats définis dans ce module. Aucun secret
n'est accepté depuis le navigateur : la clé est injectée par le serveur.
"""
from __future__ import annotations

import base64
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal, Callable

import requests


class ProviderError(RuntimeError):
    """Erreur renvoyée par un fournisseur externe."""


class ProviderUnavailable(ProviderError):
    """Provider non configuré ou modèle non accessible."""


class ProviderTimeout(ProviderError):
    """Le fournisseur n'a pas répondu dans le délai demandé."""


@dataclass(frozen=True)
class VideoJob:
    job_id: str
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    progress: float | None = None
    error: str | None = None


@dataclass(frozen=True)
class VideoArtifact:
    data: bytes
    mime_type: str
    filename: str | None = None


@dataclass(frozen=True)
class WebSearchResult:
    title: str
    url: str
    snippet: str = ""
    source: str = ""
    answer: str = ""
    queries: tuple[str, ...] = ()


@dataclass(frozen=True)
class ImageArtifact:
    data: bytes
    mime_type: str
    filename: str = "image-dashle.png"


class VideoGenerationProvider(ABC):
    @abstractmethod
    def create(self, prompt: str, *, timeout_s: float) -> VideoJob:
        raise NotImplementedError

    @abstractmethod
    def status(self, job_id: str, *, timeout_s: float) -> VideoJob:
        raise NotImplementedError

    @abstractmethod
    def retrieve(self, job_id: str, *, timeout_s: float) -> VideoArtifact:
        raise NotImplementedError

    def cancel(self, job_id: str, *, timeout_s: float) -> bool:
        raise NotImplementedError("Ce fournisseur ne prend pas en charge l'annulation.")


class WebSearchProvider(ABC):
    @abstractmethod
    def search(self, query: str, *, timeout_s: float) -> list[WebSearchResult]:
        raise NotImplementedError


class ImageGenerationProvider(ABC):
    @abstractmethod
    def generate(self, prompt: str, context: str = "", *, timeout_s: float) -> ImageArtifact:
        raise NotImplementedError


class ImageEditingProvider(ABC):
    @abstractmethod
    def edit(self, image_bytes: bytes, mime_type: str, prompt: str, *, timeout_s: float) -> ImageArtifact:
        raise NotImplementedError


def _provider_error(response: requests.Response, label: str) -> ProviderError:
    code = response.status_code
    if code in (401, 403, 404):
        return ProviderUnavailable(f"{label} indisponible (HTTP {code}).")
    detail = ""
    try:
        payload = response.json()
        detail = str((payload.get("error") or {}).get("message") or "").strip()
    except (ValueError, TypeError):
        pass
    return ProviderError(f"{label} a refusé la requête (HTTP {code})" + (f": {detail[:240]}" if detail else "."))


def _request_json(method: str, url: str, *, api_key: str, timeout_s: float, **kwargs) -> dict:
    try:
        response = requests.request(
            method,
            url,
            headers={"x-goog-api-key": api_key, **kwargs.pop("headers", {})},
            timeout=timeout_s,
            **kwargs,
        )
    except requests.Timeout as exc:
        raise ProviderTimeout("Le fournisseur a dépassé le délai autorisé.") from exc
    except requests.RequestException as exc:
        raise ProviderError("Le fournisseur est momentanément inaccessible.") from exc
    if not response.ok:
        raise _provider_error(response, "Le fournisseur Gemini")
    try:
        return response.json()
    except ValueError as exc:
        raise ProviderError("Réponse JSON invalide du fournisseur Gemini.") from exc


class ExistingGeminiImageGenerationProvider(ImageGenerationProvider):
    """Adaptateur du pipeline Gemini image existant, sans le réécrire."""
    def __init__(self, generator):
        self._generator = generator

    def generate(self, prompt: str, context: str = "", *, timeout_s: float = 90) -> ImageArtifact:
        raw, mime = self._generator(prompt, context)
        if not raw or len(raw) > 8 * 1024 * 1024 or not str(mime).startswith("image/"):
            raise ProviderError("Le pipeline image a retourné un artefact invalide.")
        return ImageArtifact(data=raw, mime_type=mime, filename="image-dashle.png")


class GeminiWebSearchProvider(WebSearchProvider):
    """Recherche Web Google Grounding via l'Interactions API Gemini."""

    def __init__(self, api_key_getter: Callable[[], str | None], model: str | None = None):
        self._api_key_getter = api_key_getter
        self.model = model or os.environ.get("GEMINI_WEB_SEARCH_MODEL", "gemini-3.5-flash-lite")

    def search(self, query: str, *, timeout_s: float = 30) -> list[WebSearchResult]:
        api_key = self._api_key_getter()
        if not api_key:
            raise ProviderUnavailable("La recherche Web n'est pas configurée.")
        body = {
            "model": self.model,
            "store": False,
            "input": query[:24000],
            "tools": [{"type": "google_search"}],
        }
        data = _request_json(
            "POST",
            "https://generativelanguage.googleapis.com/v1beta/interactions",
            api_key=api_key,
            timeout_s=timeout_s,
            json=body,
        )
        answer = str(data.get("output_text") or "").strip()
        results = []
        seen = set()
        queries = []
        for step in data.get("steps", []):
            if step.get("type") == "google_search_call":
                for q in (step.get("arguments") or {}).get("queries", []):
                    if q and q not in queries:
                        queries.append(str(q)[:300])
            if step.get("type") != "model_output":
                continue
            for block in step.get("content", []):
                if block.get("type") != "text":
                    continue
                block_text = str(block.get("text") or "").strip()
                if block_text:
                    answer = block_text
                for annotation in block.get("annotations", []):
                    if annotation.get("type") != "url_citation":
                        continue
                    url = str(annotation.get("url") or "").strip()
                    if not (url.startswith("https://") or url.startswith("http://")) or url in seen:
                        continue
                    seen.add(url)
                    start = max(0, int(annotation.get("start_index") or 0))
                    end = min(len(answer), int(annotation.get("end_index") or len(answer)))
                    results.append(WebSearchResult(
                        title=str(annotation.get("title") or url)[:240],
                        url=url[:2000],
                        snippet=answer[start:end],
                        source=str(annotation.get("title") or "").strip()[:160],
                        answer=answer,
                        queries=tuple(queries),
                    ))
        if not answer and not results:
            raise ProviderError("La recherche Web n'a retourné aucun résultat exploitable.")
        if not results:
            results.append(WebSearchResult(
                title="Recherche Web Gemini", url="", answer=answer, queries=tuple(queries)
            ))
        return results


class GeminiImageEditingProvider(ImageEditingProvider):
    """Édition image native Gemini : image source + instruction."""

    def __init__(self, api_key_getter: Callable[[], str | None], model: str | None = None):
        self._api_key_getter = api_key_getter
        self.model = model or os.environ.get("GEMINI_IMAGE_MODEL", "gemini-3.1-flash-image")

    def edit(self, image_bytes: bytes, mime_type: str, prompt: str, *, timeout_s: float = 90) -> ImageArtifact:
        api_key = self._api_key_getter()
        if not api_key:
            raise ProviderUnavailable("L'édition d'image n'est pas configurée.")
        encoded = base64.b64encode(image_bytes).decode("ascii")
        body = {
            "model": self.model,
            "store": False,
            "input": [
                {"type": "image", "mime_type": mime_type, "data": encoded},
                {"type": "text", "text": prompt[:24000]},
            ],
            "response_format": {
                "type": "image", "mime_type": "image/png",
                "aspect_ratio": "16:9", "image_size": "1K",
            },
        }
        data = _request_json(
            "POST",
            "https://generativelanguage.googleapis.com/v1beta/interactions",
            api_key=api_key,
            timeout_s=timeout_s,
            json=body,
        )
        image_data = (data.get("output_image") or {}).get("data")
        image_mime = (data.get("output_image") or {}).get("mime_type") or "image/png"
        if not image_data:
            for step in data.get("steps", []):
                if step.get("type") != "model_output":
                    continue
                for block in step.get("content", []):
                    if block.get("type") == "image" and block.get("data"):
                        image_data = block["data"]
                        image_mime = block.get("mime_type") or image_mime
                        break
                if image_data:
                    break
        if not image_data:
            raise ProviderError("Le fournisseur n'a retourné aucune image modifiée.")
        try:
            raw = base64.b64decode(image_data, validate=True)
        except (ValueError, TypeError) as exc:
            raise ProviderError("Image modifiée invalide.") from exc
        if not raw or len(raw) > 8 * 1024 * 1024:
            raise ProviderError("Image modifiée trop volumineuse.")
        return ImageArtifact(data=raw, mime_type=image_mime, filename="image-dashle-modifiee.png")


class GeminiVideoGenerationProvider(VideoGenerationProvider):
    """Veo 3.1 : création asynchrone, suivi réel de l'opération et récupération."""

    def __init__(self, api_key_getter: Callable[[], str | None], model: str | None = None):
        self._api_key_getter = api_key_getter
        self.model = model or os.environ.get("GEMINI_VIDEO_MODEL", "veo-3.1-generate-preview")

    def create(self, prompt: str, *, timeout_s: float = 30) -> VideoJob:
        api_key = self._api_key_getter()
        if not api_key:
            raise ProviderUnavailable("La génération vidéo n'est pas configurée.")
        body = {
            "instances": [{"prompt": prompt[:24000]}],
            "parameters": {
                "aspectRatio": os.environ.get("GEMINI_VIDEO_ASPECT_RATIO", "16:9"),
                "resolution": os.environ.get("GEMINI_VIDEO_RESOLUTION", "720p"),
                "durationSeconds": os.environ.get("GEMINI_VIDEO_DURATION_SECONDS", "8"),
            },
        }
        data = _request_json(
            "POST",
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:predictLongRunning",
            api_key=api_key,
            timeout_s=timeout_s,
            json=body,
        )
        name = str(data.get("name") or "").strip()
        if not name:
            raise ProviderError("Veo n'a pas retourné d'identifiant de job.")
        return VideoJob(job_id=name, status="queued", progress=None)

    def status(self, job_id: str, *, timeout_s: float = 20) -> VideoJob:
        api_key = self._api_key_getter()
        if not api_key:
            raise ProviderUnavailable("La génération vidéo n'est pas configurée.")
        data = _request_json(
            "GET",
            "https://generativelanguage.googleapis.com/v1beta/" + job_id.lstrip("/"),
            api_key=api_key,
            timeout_s=timeout_s,
        )
        if data.get("done") is True:
            if data.get("error"):
                return VideoJob(job_id=job_id, status="failed", progress=None,
                                 error="Le fournisseur vidéo a échoué.")
            return VideoJob(job_id=job_id, status="succeeded", progress=100.0)
        metadata = data.get("metadata") or {}
        progress = metadata.get("progressPercent")
        try:
            progress = float(progress) if progress is not None else None
        except (TypeError, ValueError):
            progress = None
        return VideoJob(job_id=job_id, status="running", progress=progress)

    def retrieve(self, job_id: str, *, timeout_s: float = 30) -> VideoArtifact:
        api_key = self._api_key_getter()
        if not api_key:
            raise ProviderUnavailable("La génération vidéo n'est pas configurée.")
        data = _request_json(
            "GET",
            "https://generativelanguage.googleapis.com/v1beta/" + job_id.lstrip("/"),
            api_key=api_key,
            timeout_s=timeout_s,
        )
        samples = (((data.get("response") or {}).get("generateVideoResponse") or {})
                   .get("generatedSamples") or [])
        uri = (samples[0].get("video") or {}).get("uri") if samples else None
        if not uri:
            raise ProviderError("Veo n'a pas retourné de fichier vidéo.")
        try:
            response = requests.get(uri, headers={"x-goog-api-key": api_key}, timeout=timeout_s)
        except requests.Timeout as exc:
            raise ProviderTimeout("Téléchargement vidéo trop long.") from exc
        except requests.RequestException as exc:
            raise ProviderError("Téléchargement vidéo impossible.") from exc
        if not response.ok:
            raise _provider_error(response, "Le téléchargement vidéo")
        if not response.content or len(response.content) > 100 * 1024 * 1024:
            raise ProviderError("Vidéo reçue vide ou trop volumineuse.")
        return VideoArtifact(data=response.content, mime_type="video/mp4", filename="video-dashle.mp4")


class ProviderRegistry:
    """Registre unique : disponibilité et contrats sont centralisés côté serveur."""

    def __init__(self, api_key_getter: Callable[[], str | None], image_generator=None):
        self._providers = {
            "image_generation": ExistingGeminiImageGenerationProvider(image_generator) if image_generator else None,
            "web_search": GeminiWebSearchProvider(api_key_getter),
            "image_editing": GeminiImageEditingProvider(api_key_getter),
            "video_generation": GeminiVideoGenerationProvider(api_key_getter),
        }
        self._api_key_getter = api_key_getter

    def get(self, tool: str):
        return self._providers.get(tool)

    def available(self, tool: str) -> bool:
        return bool(self.get(tool)) and bool(self._api_key_getter())

    def status(self) -> dict[str, str]:
        return {name: ("configured" if self.available(name) else "provider_unavailable")
                for name in self._providers}
