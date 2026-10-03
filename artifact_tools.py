"""Outils sécurisés de génération de documents et d'images pour Dashle.

La couche reçoit une intention utilisateur, demande à Gemini une structure
exploitable pour les documents, puis rend le fichier côté serveur. Les clés API
ne quittent jamais le serveur et aucun code utilisateur n'est exécuté.
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import unicodedata
from html import escape as html_escape
from typing import Any

import requests
from PIL import Image, UnidentifiedImageError

from config import CLE_API, MODELE_GEMINI

MODELE_IMAGE = os.environ.get("GEMINI_IMAGE_MODEL", "gemini-3.1-flash-image")
MAX_ARTIFACT_SOURCE_CHARS = 24_000
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_DOCUMENT_SECTIONS = 40
MAX_DOCUMENT_ROWS = 100
MAX_DOCUMENT_CELLS = 1000


def _normaliser(texte: str) -> str:
    return re.sub(r"\s+", " ", str(texte or "").strip().lower())


def detecter_demande_pdf(message: str) -> bool:
    """Détecte les demandes PDF courantes, sans dépendre du sujet demandé."""
    t = _normaliser(message)
    if not re.search(r"\bpdf\b", t):
        return False
    return bool(re.search(
        r"\b(g[eé]n[eéè]re?r?|cr[eé]e?r?|fais(?:-moi)?|faire|fabriquer|"
        r"transforme?r?|exporte?r?|pr[eé]pare?r?|mets?|mettre|produis|"
        r"produire|convertis?|convertir|t[eé]l[eé]charge?r?|rapport|"
        r"facture|cours|lettre|cv)\b",
        t,
    ))


def demande_pdf_sans_sujet(message: str) -> bool:
    """Retourne vrai pour une demande de PDF explicite mais sans sujet."""
    t = _normaliser(message)
    if not detecter_demande_pdf(message):
        return False
    # Les formulations de type « peux-tu me générer un PDF ? » ne donnent
    # aucun contenu à produire : Dashle doit confirmer la capacité puis demander
    # le sujet, au lieu de fabriquer un document arbitraire.
    return bool(re.fullmatch(
        r"(?:peux[- ]tu|pourrais[- ]tu|est[- ]ce que tu peux|"
        r"tu peux|peut[- ]tu)\s+(?:me\s+)?(?:g[eé]n[eéè]re?r?|"
        r"cr[eé]e?r?|fais(?:-moi)?|faire|fabriquer|produire|produis)\s+"
        r"(?:un|une)?\s*pdf(?:\s+s.?il te plait|\s+stp)?\s*\??",
        t,
    ))


def detecter_demande_image(message: str) -> bool:
    """Détecte une demande de génération d'image, y compris les formes conjuguées."""
    t = _normaliser(message)
    t_sans_accents = re.sub(r"[\u0300-\u036f]", "", unicodedata.normalize("NFD", t))
    verbe_image = re.search(
        r"\b(?:gener(?:e|es|ez|er|ee|ees|es)|cre(?:e|es|ez|er|ee|ees|es)|"
        r"fais|faire|dessin(?:e|es|ez|er)?|illustr(?:e|es|ez|er)?|"
        r"montre|represent(?:e|es|ez|er)?)\b",
        t_sans_accents,
    )
    objet_image = re.search(
        r"\b(?:image|illustration|logo|affiche|schema|diagramme|infographie|visuel|dessin)\b",
        t_sans_accents,
    )
    # Les demandes naturelles omettent souvent le mot « image » : « fais-moi
    # une ville futuriste », « dessine un portrait », etc. Limiter ce cas aux
    # sujets visuels connus évite de router toute demande créative vers Gemini.
    sujet_visuel = re.search(
        r"\b(?:ville|paysage|portrait|personnage|voiture|maison|chateau|"
        r"foret|montagne|planete|galaxie|robot|animal|fleur|bande dessinee|"
        r"scene|carte|avatar)\b",
        t_sans_accents,
    )
    intention_visuelle = re.search(
        r"\b(?:futurist\w*|volant\w*|en 3d|style|colore\w*|"
        r"realist\w*|fantast\w*|onir\w*|magnifi\w*|dessin\w*)\b",
        t_sans_accents,
    )
    return bool(verbe_image and (objet_image or (sujet_visuel and intention_visuelle)))


def detecter_demande_generation_video(message: str) -> bool:
    """Recognize explicit video creation requests; no video provider is wired yet."""
    t = _normaliser(message)
    t = "".join(
        caractere for caractere in unicodedata.normalize("NFD", t)
        if unicodedata.category(caractere) != "Mn"
    )
    creation = re.search(
        r"\b(?:genere(?:r|e|es|ez)?|cree(?:r|e|es|ez)?|fabrique(?:r|e|es|ez)?|"
        r"produi(?:s|re|t|sez)|realise(?:r|e|es|ez)?|fais(?:-moi)?|faire)\b", t
    )
    video = re.search(r"\b(?:video|film|clip|animation)\b", t)
    return bool(creation and video)


def detecter_demande_recherche_web(message: str) -> bool:
    """Recognize explicit general web browsing requests, separate from live feeds."""
    t = _normaliser(message)
    t = "".join(
        caractere for caractere in unicodedata.normalize("NFD", t)
        if unicodedata.category(caractere) != "Mn"
    )
    explicite = re.search(
        r"\b(?:sur internet|sur le web|sur google|dans le web|recherche web|"
        r"recherche sur internet|recherche sur le web|cherche sur internet|"
        r"cherche sur le web|navigue sur|ouvre des sites|trouve des sources web)\b", t
    )
    recherche = re.search(r"\b(?:cherche|recherche|trouve|recherche-moi)\b", t)
    cible_web = re.search(
        r"\b(?:dernieres? nouvelles?|actualites?|articles?|informations?|"
        r"sources?)\b", t
    )
    contexte_cible = re.search(r"\b(?:sur|concernant|a propos de)\b", t)
    return bool(explicite or (recherche and cible_web and contexte_cible))


def detecter_demande_modification_image(message: str) -> bool:
    """Recognize explicit image editing requests separately from image analysis."""
    t = _normaliser(message)
    t = "".join(
        caractere for caractere in unicodedata.normalize("NFD", t)
        if unicodedata.category(caractere) != "Mn"
    )
    return bool(re.search(
        r"\b(?:modifie|modifier|retouche|retoucher|transforme|transformer|"
        r"edite|editer|change|changer|ajoute|ajouter|supprime|supprimer)\b", t
    ))


def demande_illustration_pedagogique(message: str) -> bool:
    t = _normaliser(message)
    if detecter_demande_image(message):
        return False
    return bool(
        re.search(r"\b(explique|comment fonctionne|fonctionnement|processus|architecture|syst[eè]me solaire|concept|notion|comparaison|g[eé]ographique|scientifique|m[eé]canisme)\b", t)
        and len(t) >= 35
    )


def extraire_contenu_fourni(message: str) -> str:
    """Extrait un texte explicitement fourni sans le réécrire."""
    brut = str(message or "").strip()
    motifs = [
        r"(?:texte|contenu)\s*[:：]\s*(.+)$",
        r"(?:texte|contenu)\s+(?:suivant|ci-dessous)\s*[:：]?\s*(.+)$",
    ]
    for motif in motifs:
        m = re.search(motif, brut, flags=re.IGNORECASE | re.DOTALL)
        if m and m.group(1).strip():
            return m.group(1).strip()[:MAX_ARTIFACT_SOURCE_CHARS]
    lignes = brut.splitlines()
    if len(lignes) >= 2 and re.search(r"transforme|mets|convert", lignes[0], re.I):
        return "\n".join(lignes[1:]).strip()[:MAX_ARTIFACT_SOURCE_CHARS]
    return ""


def _schema_document() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "author": {"type": "string"},
            "language": {"type": "string"},
            "orientation": {"type": "string", "enum": ["portrait", "landscape"]},
            "footer": {"type": "string"},
            "sections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "heading": {"type": "string"},
                        "paragraphs": {"type": "array", "items": {"type": "string"}},
                        "bullets": {"type": "array", "items": {"type": "string"}},
                        "table": {
                            "type": "object",
                            "properties": {
                                "headers": {"type": "array", "items": {"type": "string"}},
                                "rows": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}},
                            },
                            "required": ["headers", "rows"],
                        },
                    },
                    "required": ["heading", "paragraphs", "bullets"],
                },
            },
        },
        "required": ["title", "author", "language", "orientation", "footer", "sections"],
    }


def _json_from_gemini(prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
    if not CLE_API:
        raise RuntimeError("GEMINI_API_KEY non configurée")
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt[:MAX_ARTIFACT_SOURCE_CHARS]}]}],
        "generationConfig": {
            "temperature": 0.35,
            "maxOutputTokens": 8192,
            "responseMimeType": "application/json",
            "responseSchema": schema,
        },
    }
    response = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{MODELE_GEMINI}:generateContent",
        params={"key": CLE_API},
        json=body,
        timeout=45,
    )
    response.raise_for_status()
    data = response.json()
    texte = data["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(texte)


def structurer_document(demande: str, contexte: str = "", contenu_fourni: str = "") -> dict[str, Any]:
    """Produit une structure de document, jamais le PDF lui-même."""
    if contenu_fourni:
        titre = "Document Dashle"
        premiere = next((x.strip() for x in contenu_fourni.splitlines() if x.strip()), titre)
        if len(premiere) <= 160:
            titre = premiere
        return {
            "title": titre,
            "author": "",
            "language": "fr",
            "orientation": "portrait",
            "footer": "Document préparé par DASHLE",
            "sections": [{"heading": "", "paragraphs": [contenu_fourni], "bullets": []}],
        }
    prompt = f"""Tu es le moteur de préparation de documents de DASHLE.
Comprends la demande et retourne UNIQUEMENT un JSON conforme au schéma fourni.
Le document doit contenir le contenu demandé, pas une paraphrase de la commande.
Respecte la langue, le titre, la longueur, les sections, listes et tableaux demandés.
Si l'utilisateur demande 10 règles, produis réellement 10 règles.
Si une information n'est pas connue, n'invente pas de donnée présentée comme factuelle.
Contexte conversationnel pertinent:
{str(contexte or '')[:8000]}

Demande:
{demande[:MAX_ARTIFACT_SOURCE_CHARS]}
"""
    return _valider_structure(_json_from_gemini(prompt, _schema_document()))


def _valider_structure(document: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise ValueError("Structure de document invalide")
    document["title"] = str(document.get("title") or "Document Dashle")[:200]
    document["author"] = str(document.get("author") or "")[:160]
    document["language"] = str(document.get("language") or "fr")[:20]
    document["orientation"] = "landscape" if document.get("orientation") == "landscape" else "portrait"
    document["footer"] = str(document.get("footer") or "")[:200]
    sections = document.get("sections")
    if not isinstance(sections, list):
        sections = []
    resultat = []
    for section in sections[:MAX_DOCUMENT_SECTIONS]:
        if not isinstance(section, dict):
            continue
        paragraphs = [str(x)[:12000] for x in section.get("paragraphs", []) if str(x).strip()][:100]
        bullets = [str(x)[:2000] for x in section.get("bullets", []) if str(x).strip()][:100]
        table = section.get("table")
        table_ok = None
        if isinstance(table, dict):
            headers = [str(x)[:200] for x in table.get("headers", [])][:20]
            rows = []
            for row in table.get("rows", [])[:MAX_DOCUMENT_ROWS]:
                if isinstance(row, list):
                    rows.append([str(x)[:500] for x in row[:len(headers) or 20]])
            if headers and rows and sum(len(r) for r in rows) <= MAX_DOCUMENT_CELLS:
                table_ok = {"headers": headers, "rows": rows}
        resultat.append({"heading": str(section.get("heading") or "")[:200],
                         "paragraphs": paragraphs, "bullets": bullets, "table": table_ok})
    document["sections"] = resultat or [{"heading": "", "paragraphs": ["Document préparé par DASHLE."], "bullets": []}]
    return document


def rendre_pdf(document: dict[str, Any], image_bytes: bytes | None = None) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Image as PDFImage, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    doc = _valider_structure(document)
    pagesize = landscape(A4) if doc["orientation"] == "landscape" else A4
    out = io.BytesIO()
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="DashleDocTitle", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=22, leading=27, spaceAfter=14, alignment=TA_LEFT))
    styles.add(ParagraphStyle(name="DashleDocH", parent=styles["Heading2"], fontName="Helvetica-Bold", fontSize=14, leading=18, spaceBefore=10, spaceAfter=6))
    styles.add(ParagraphStyle(name="DashleDocBody", parent=styles["BodyText"], fontName="Helvetica", fontSize=10.5, leading=15, spaceAfter=7))
    styles.add(ParagraphStyle(name="DashleDocBullet", parent=styles["BodyText"], fontName="Helvetica", fontSize=10.5, leading=15, leftIndent=14, firstLineIndent=-8, spaceAfter=4))
    story = [Paragraph(html_escape(doc["title"]), styles["DashleDocTitle"])]
    if doc["author"]:
        story.append(Paragraph(html_escape(doc["author"]), styles["DashleDocBody"]))
    if image_bytes:
        try:
            image = Image.open(io.BytesIO(image_bytes))
            image.verify()
            image = Image.open(io.BytesIO(image_bytes))
            image.thumbnail((155 * mm, 90 * mm))
            image_buf = io.BytesIO()
            image.convert("RGB").save(image_buf, format="PNG")
            image_buf.seek(0)
            story.extend([PDFImage(image_buf, width=image.width * 0.264583, height=image.height * 0.264583), Spacer(1, 6)])
        except (UnidentifiedImageError, OSError, ValueError):
            pass
    for section in doc["sections"]:
        if section["heading"]:
            story.append(Paragraph(html_escape(section["heading"]), styles["DashleDocH"]))
        for paragraph in section["paragraphs"]:
            for bloc in str(paragraph).split("\n"):
                if bloc.strip():
                    story.append(Paragraph(html_escape(bloc), styles["DashleDocBody"]))
        for bullet in section["bullets"]:
            story.append(Paragraph("• " + html_escape(bullet), styles["DashleDocBullet"]))
        table = section.get("table")
        if table:
            data = [[Paragraph(html_escape(x), styles["DashleDocBody"]) for x in table["headers"]]]
            data += [[Paragraph(html_escape(x), styles["DashleDocBody"]) for x in row] for row in table["rows"]]
            tbl = Table(data, repeatRows=1)
            tbl.setStyle(TableStyle([
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ]))
            story.extend([Spacer(1, 5), tbl, Spacer(1, 8)])
    footer = doc["footer"]
    def pied_de_page(canvas, _doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.drawString(18 * mm, 10 * mm, footer[:200])
        canvas.drawRightString(pagesize[0] - 18 * mm, 10 * mm, f"Page {canvas.getPageNumber()}")
        canvas.restoreState()
    SimpleDocTemplate(out, pagesize=pagesize, rightMargin=18*mm, leftMargin=18*mm, topMargin=16*mm, bottomMargin=18*mm, title=doc["title"], author=doc["author"] or "DASHLE").build(story, onFirstPage=pied_de_page, onLaterPages=pied_de_page)
    data = out.getvalue()
    if not data.startswith(b"%PDF-") or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("PDF invalide ou trop volumineux")
    return data


def generer_image(prompt: str, contexte: str = "") -> tuple[bytes, str]:
    if not CLE_API:
        raise RuntimeError("GEMINI_API_KEY non configurée")
    texte = f"""Génère l'image demandée par l'utilisateur.
Utilise le contexte uniquement pour résoudre les références ambiguës.
Ne réponds pas avec une description : produis réellement une image.
Demande: {prompt[:MAX_ARTIFACT_SOURCE_CHARS]}
Contexte: {str(contexte or '')[:8000]}"""
    body = {
        "model": MODELE_IMAGE,
        "input": [{"type": "text", "text": texte}],
        "response_format": {"type": "image", "mime_type": "image/png", "aspect_ratio": "16:9", "image_size": "1K"},
    }
    response = requests.post(
        "https://generativelanguage.googleapis.com/v1beta/interactions",
        headers={"x-goog-api-key": CLE_API, "Content-Type": "application/json"},
        json=body, timeout=90,
    )
    if not response.ok:
        detail = ""
        try:
            payload_erreur = response.json()
            erreur = payload_erreur.get("error", {})
            detail = str(erreur.get("message") or "").strip()
        except (ValueError, TypeError):
            detail = ""
        suffixe = f": {detail}" if detail else ""
        raise RuntimeError(
            f"Le fournisseur de génération d'image a refusé la requête (HTTP {response.status_code}){suffixe}"
        )
    payload = response.json()
    data = payload.get("output_image", {}).get("data")
    mime = payload.get("output_image", {}).get("mime_type", "image/png")
    if not data:
        for step in payload.get("steps", []):
            for block in step.get("content", []):
                if block.get("type") == "image" and block.get("data"):
                    data, mime = block["data"], block.get("mime_type", "image/png")
                    break
            if data:
                break
    if not data:
        raise ValueError("Le modèle n'a pas retourné d'image")
    raw = base64.b64decode(data, validate=True)
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("Image générée trop volumineuse")
    try:
        image = Image.open(io.BytesIO(raw))
        image.verify()
        image = Image.open(io.BytesIO(raw))
        fmt = image.format or "PNG"
        mime = f"image/{fmt.lower()}"
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ValueError("Image générée invalide") from exc
    return raw, mime


def extraire_texte_structure(document: dict[str, Any]) -> str:
    lignes = [document.get("title", "")]
    for section in document.get("sections", []):
        if section.get("heading"):
            lignes.append(section["heading"])
        lignes.extend(section.get("paragraphs", []))
        lignes.extend("- " + x for x in section.get("bullets", []))
        table = section.get("table")
        if table:
            lignes.append(" | ".join(table["headers"]))
            lignes.extend(" | ".join(row) for row in table["rows"])
    return "\n\n".join(x for x in lignes if x).strip()
