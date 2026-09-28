"""Validation and bounded text extraction for persistent project attachments."""

from io import BytesIO
from pathlib import Path


MAX_EXTRACTED_TEXT_CHARS = 100_000
MAX_PDF_PAGES = 100

_MIME_TYPES = {
    ".csv": "text/csv",
    ".md": "text/markdown",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
}


def extract_project_file(filename: str, content: bytes) -> tuple[str, str]:
    """Return a verified MIME type and bounded searchable text for a file."""
    extension = Path(filename).suffix.lower()
    mime_type = _MIME_TYPES.get(extension)
    if not mime_type:
        raise ValueError("Formats acceptés : CSV, PDF, TXT et Markdown.")
    if not content:
        raise ValueError("Le fichier est vide.")

    if extension == ".pdf":
        if not content.startswith(b"%PDF-"):
            raise ValueError("Le fichier PDF est invalide.")
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise ValueError("La lecture PDF est indisponible sur ce serveur.") from exc
        try:
            reader = PdfReader(BytesIO(content), strict=False)
            if reader.is_encrypted:
                raise ValueError("Les PDF protégés par mot de passe ne sont pas acceptés.")
            pages = reader.pages[:MAX_PDF_PAGES]
            extracted = "\n".join(page.extract_text() or "" for page in pages)
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("Le contenu de ce PDF n’a pas pu être lu.") from exc
        if not extracted.strip():
            raise ValueError("Ce PDF ne contient pas de texte exploitable.")
    else:
        if b"\x00" in content:
            raise ValueError("Le fichier texte contient des données binaires invalides.")
        try:
            extracted = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                extracted = content.decode("cp1252")
            except UnicodeDecodeError as exc:
                raise ValueError("Le fichier texte n’utilise pas un encodage compatible.") from exc

    return mime_type, extracted[:MAX_EXTRACTED_TEXT_CHARS]
