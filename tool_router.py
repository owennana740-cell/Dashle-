"""Routage d'intentions naturelles vers les outils Dashle."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class ToolIntent:
    name: str
    reason: str = ""


def _norm(text: str) -> str:
    value = unicodedata.normalize("NFD", str(text or "").lower())
    value = "".join(c for c in value if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", value).strip()


def _has(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text) for pattern in patterns)


def detect_tool_intent(message: str, *, has_image: bool = False, has_video: bool = False) -> ToolIntent:
    t = _norm(message)
    if not t:
        return ToolIntent("chat")
    if _has(t, (r"\bpdf\b",)) and _has(t, (
        r"\b(genere|generer|cree|creer|fais|faire|fabrique|exporte|exporter|transforme|converti|prepare|produi)\w*\b",
    )):
        return ToolIntent("pdf_generation", "demande explicite de document PDF")
    if _has(t, (
        r"\bvideo\b.*\b(genere|cree|fais|fabrique|produi|realise)\w*\b",
        r"\b(genere|cree|fais|fabrique|produi|realise)\w*\b.*\bvideo\b",
        r"\b(film|clip|animation)\b.*\b(genere|cree|fais|fabrique|realise)\w*\b",
    )):
        return ToolIntent("video_generation", "création vidéo")
    if has_video and _has(t, (
        r"\b(analyse|analyser|explique|decris|resume|resumer|comprends)\b",
        r"qu'est-ce qui se passe",
    )):
        return ToolIntent("video_analysis", "analyse du média fourni")
    if _has(t, (
        r"\b(sur internet|sur le web|sur google|recherche web|recherche sur internet|cherche sur internet|cherche sur le web|sources web)\b",
        r"\b(cherche|recherche|trouve)\w*\b.*\b(derni|actualite|nouvelles|articles|sources|informations)\b",
        r"\b(derni|actualite|nouvelles|articles|sources|informations)\b.*\b(cherche|recherche|trouve)\w*\b",
    )):
        return ToolIntent("web_search", "demande de recherche fraîche sur le Web")
    if has_image and _has(t, (
        r"\b(modifie|modifier|retouche|retoucher|transforme|transformer|edite|editer|change|changer|ajoute|ajouter|supprime|supprimer)\b",
        r"\b(style|couleur|fond|arriere-plan)\b.*\b(image|photo)\b",
    )):
        return ToolIntent("image_editing", "instruction appliquée à l'image fournie")
    if _has(t, (
        r"\b(genere|generer|cree|creer|dessine|dessiner|illustre|illustrer|fais|faire|represente|representer)\w*\b",
    )) and _has(t, (
        r"\b(image|illustration|logo|affiche|schema|diagramme|infographie|visuel|dessin|portrait|paysage|ville|voiture|personnage|avatar)\b",
    )):
        return ToolIntent("image_generation", "création d'un visuel")
    if has_image and _has(t, (
        r"\b(analyse|analyser|decris|explique|qu'est-ce|que vois)\b",
    )):
        return ToolIntent("image_analysis", "analyse du média fourni")
    return ToolIntent("chat")
