"""Contrats côté serveur pour brancher de futurs fournisseurs d'outils.

Ces interfaces ne fournissent aucun résultat elles-mêmes. Une implémentation
doit être configurée côté serveur avant que le routage puisse l'utiliser.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal


class ProviderError(RuntimeError):
    """Erreur renvoyée par un fournisseur externe."""


class ProviderTimeout(ProviderError):
    """Le fournisseur n'a pas répondu dans le délai demandé."""


@dataclass(frozen=True)
class VideoJob:
    job_id: str
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    # None signifie que le fournisseur ne publie pas de progression mesurable.
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


class VideoGenerationProvider(ABC):
    """Contrat asynchrone vidéo; la progression est uniquement celle du provider."""

    @abstractmethod
    def create(self, prompt: str, *, timeout_s: float) -> VideoJob:
        """Créer une génération réelle ou lever ProviderError/ProviderTimeout."""

    @abstractmethod
    def status(self, job_id: str, *, timeout_s: float) -> VideoJob:
        """Lire l'état et, si le provider en donne une, sa progression réelle."""

    @abstractmethod
    def retrieve(self, job_id: str, *, timeout_s: float) -> VideoArtifact:
        """Récupérer le fichier produit après succès."""

    def cancel(self, job_id: str, *, timeout_s: float) -> bool:
        """Annuler si pris en charge; l'implémentation peut lever NotImplementedError."""
        raise NotImplementedError("Ce fournisseur ne prend pas en charge l'annulation.")


class WebSearchProvider(ABC):
    """Contrat serveur pour une recherche Web réelle et attribuable."""

    @abstractmethod
    def search(self, query: str, *, timeout_s: float) -> list[WebSearchResult]:
        """Retourner des résultats provenant du moteur configuré."""
