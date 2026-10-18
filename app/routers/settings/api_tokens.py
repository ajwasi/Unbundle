from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app import api_tokens as api_tokens_lib
from app.csrf import require_csrf
from app.deps import get_db
from app.models.api_token import ApiToken
from app.templates_env import templates

router = APIRouter()


def api_context(db: Session, new_token_plaintext: str | None = None) -> dict:
    return {
        "api_tokens": db.query(ApiToken).order_by(ApiToken.created_at.desc()).all(),
        "new_token_plaintext": new_token_plaintext,
    }


@router.post("/api-tokens", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def create_api_token(request: Request, name: str = Form(""), db: Session = Depends(get_db)):
    name = name.strip()
    new_token_plaintext = None
    if name:
        _row, new_token_plaintext = api_tokens_lib.create_token(db, name)
    return templates.TemplateResponse(request, "settings/_api_form.html", api_context(db, new_token_plaintext))


@router.post("/api-tokens/{token_id}/delete", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def delete_api_token(request: Request, token_id: int, db: Session = Depends(get_db)):
    api_tokens_lib.delete_token(db, token_id)
    return templates.TemplateResponse(request, "settings/_api_form.html", api_context(db))
