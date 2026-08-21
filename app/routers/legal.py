from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["legal"])

_LEGAL_DIR = Path(__file__).parent.parent / "legal"

_PRIVACY_HTML = (_LEGAL_DIR / "privacy.html").read_text(encoding="utf-8")
_TERMS_HTML = (_LEGAL_DIR / "terms.html").read_text(encoding="utf-8")

@router.get("/privacy", response_class=HTMLResponse)
async def privacy_policy() -> str:
    """Endpoint for hosting privacy policy for Twilio requirements"""
    return _PRIVACY_HTML

@router.get("/terms", response_class=HTMLResponse)
async def terms() -> str:
    """Endpoint for hosting terms and conditions for Twilio requirements"""
    return _TERMS_HTML