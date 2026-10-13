"""OCR adapter. Self-hosted only: it shells out to a local `tesseract` binary and never calls a network OCR or
vision service, so document content does not leave our infrastructure. Absent binary = a clear degraded outcome."""

from __future__ import annotations

import shutil
import subprocess

NEEDS_OCR = "scanned PDF needs OCR; no OCR engine available"
_TESSERACT_LANGS = {"en": "eng", "hi": "hin", "ar": "ara", "he": "heb", "zh": "chi_sim", "ja": "jpn", "th": "tha",
                    "es": "spa", "fr": "fra", "de": "deu", "pt": "por", "ta": "tam", "te": "tel", "bn": "ben"}
IMAGE_TIMEOUT_S = 30.0


def ocr_available() -> bool:
    return shutil.which("tesseract") is not None


def ocr_image(image: bytes, language: str | None = None) -> str:
    """Text of one page image. Raises RuntimeError on engine failure (the caller treats the page as unreadable)."""
    binary = shutil.which("tesseract")
    if binary is None:
        raise RuntimeError(NEEDS_OCR)
    cmd = [binary, "stdin", "stdout"]
    lang = _TESSERACT_LANGS.get(language or "")
    if lang:
        cmd += ["-l", lang]
    proc = subprocess.run(cmd, input=image, capture_output=True, timeout=IMAGE_TIMEOUT_S, check=False)
    if proc.returncode != 0:
        raise RuntimeError("OCR engine failed on a page")
    return proc.stdout.decode("utf-8", errors="replace")
