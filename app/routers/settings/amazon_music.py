from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.connectors import amazon_music_probe, amazon_music_template
from app.csrf import require_csrf
from app.deps import get_db
from app.models.credential import SOURCE_AUDIBLE, Credential
from app.ratelimit import RateLimiter, rate_limit
from app.templates_env import templates

router = APIRouter()

# Each press makes exactly one request to Amazon, and this is an
# undocumented API on a personal account — same ceiling as every other
# outbound refresh in this app.
_music_probe_limiter = RateLimiter(max_calls=5, period_seconds=60)


def amazon_music_context(db: Session, result=None, error: str | None = None) -> dict:
    """The Amazon Music card is a reachability probe, not a connector yet — it
    reports whether music.amazon.com answers httpx with JSON or a challenge.
    `audible_connected` drives the whole card: option A derives its cookies
    from the stored Audible login, so with no Audible credential there is
    nothing to probe with.
    """
    template = amazon_music_template.load_template(db)
    return {
        "music_audible_connected": Credential.get(db, SOURCE_AUDIBLE) is not None,
        "music_result": result,
        "music_error": error,
        # Only ever the capture date — the template itself holds device and
        # customer identifiers and is never rendered back.
        "music_template_captured_at": template.captured_at if template else "",
        # Rendered from the constant the validator actually uses, so the help
        # text cannot drift out of sync with what a paste is allowed to target
        # — it already did once, telling the user a valid capture would be
        # rejected.
        "music_allowed_domains": sorted(amazon_music_probe.ALLOWED_REPLAY_DOMAINS),
    }


def _music_response(request: Request, db: Session, result=None, error: str | None = None):
    return templates.TemplateResponse(
        request, "settings/_amazon_music_form.html", amazon_music_context(db, result, error)
    )


def _music_probe(request: Request, db: Session, run):
    """Shared shape for both probe buttons: derive cookies from the stored
    Audible login, run `run(cookies)`, and render whatever came back. Defined
    as a plain `def` route below so FastAPI runs the blocking httpx call in a
    threadpool instead of stalling the event loop.
    """
    try:
        cookies = amazon_music_probe.cookies_from_audible_credential(db)
        return _music_response(request, db, result=run(cookies))
    except amazon_music_probe.NotConnectedError as exc:
        return _music_response(request, db, error=str(exc))
    except amazon_music_probe.ProbeError as exc:
        return _music_response(request, db, error=str(exc))
    except ValueError as exc:  # parse_curl rejected the pasted command
        return _music_response(request, db, error=str(exc))


@router.post(
    "/amazon-music/test",
    response_class=HTMLResponse,
    dependencies=[Depends(rate_limit(_music_probe_limiter, "amazon-music-probe")), Depends(require_csrf)],
)
def test_amazon_music(request: Request, db: Session = Depends(get_db)):
    return _music_probe(request, db, amazon_music_probe.probe_config)


@router.post(
    "/amazon-music/replay",
    response_class=HTMLResponse,
    dependencies=[Depends(rate_limit(_music_probe_limiter, "amazon-music-probe")), Depends(require_csrf)],
)
def replay_amazon_music(request: Request, curl_text: str = Form(""), db: Session = Depends(get_db)):
    return _music_probe(request, db, lambda cookies: amazon_music_probe.replay_curl(curl_text, cookies))


@router.post(
    "/amazon-music/template",
    response_class=HTMLResponse,
    dependencies=[Depends(rate_limit(_music_probe_limiter, "amazon-music-probe")), Depends(require_csrf)],
)
def save_amazon_music_template(request: Request, curl_text: str = Form(""), db: Session = Depends(get_db)):
    """Store a captured request's shape, after proving it actually works.

    Saving an unverified capture would trade one silent failure for another,
    so the paste is replayed first and only a real JSON response is accepted.
    """
    try:
        template = amazon_music_template.build_template(
            curl_text, datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        )
        cookies = amazon_music_probe.cookies_from_audible_credential(db)
        result = amazon_music_probe.replay_curl(curl_text, cookies)
    except amazon_music_template.TemplateError as exc:
        return _music_response(request, db, error=str(exc))
    except amazon_music_probe.NotConnectedError as exc:
        return _music_response(request, db, error=str(exc))
    except (amazon_music_probe.ProbeError, ValueError) as exc:
        return _music_response(request, db, error=str(exc))

    if not result.ok:
        return _music_response(
            request,
            db,
            result=result,
            error="That capture did not come back as JSON, so it was not saved — a template that "
            "does not work would only move the failure somewhere harder to see.",
        )

    amazon_music_template.save_template(db, template)
    return _music_response(request, db, result=result)


@router.post("/amazon-music/template/clear", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def clear_amazon_music_template(request: Request, db: Session = Depends(get_db)):
    amazon_music_template.clear_template(db)
    return _music_response(request, db)
