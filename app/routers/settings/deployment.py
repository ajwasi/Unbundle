from fastapi import Request

from app.config import settings


def deployment_context(request: Request) -> dict:
    """Read-only view of how this request actually arrived, so a misconfigured
    reverse proxy is visible here instead of only as a confusing failure
    somewhere else.

    Deliberately not editable: TLS settings are read by uvicorn at boot and
    can't change at runtime, and a Secure-cookie toggle in the UI is a
    lockout — flip it on without real HTTPS and the browser stops sending the
    cookie needed to reach the page that would flip it back.
    """
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    scheme = request.url.scheme
    warnings = []

    # The silent one. uvicorn only trusts X-Forwarded-* from FORWARDED_ALLOW_IPS
    # (default: 127.0.0.1, which a proxy in its own container never matches), so
    # without it every URL this app generates — the OIDC redirect URI right
    # above, most of all — claims http:// and an internal host.
    if forwarded_proto == "https" and scheme != "https":
        warnings.append(
            "Your proxy is sending X-Forwarded-Proto: https, but this app still "
            "sees the request as http — it isn't trusting that header. Set "
            "FORWARDED_ALLOW_IPS=* on the app service (docker-compose.yml "
            "already does). Until then, the OIDC redirect URI shown above is "
            "wrong, and your identity provider will reject it."
        )
    if scheme == "https" and not settings.https_enabled:
        warnings.append(
            "This app is reached over HTTPS, but its session and CSRF cookies "
            "are not marked Secure, so a browser would still send them over "
            "plain HTTP. Set BEHIND_HTTPS_PROXY=true."
        )

    if settings.ssl_certfile:
        tls = "Terminated by this app (SSL_CERTFILE)"
    elif settings.behind_https_proxy:
        tls = "Terminated by a reverse proxy (BEHIND_HTTPS_PROXY)"
    else:
        tls = "None — plain HTTP"

    return {
        "deploy_scheme": scheme,
        "deploy_host": request.url.netloc,
        "deploy_tls": tls,
        "deploy_cookies_secure": settings.https_enabled,
        "deploy_forwarded_proto": forwarded_proto,
        "deploy_forwarded_for": request.headers.get("x-forwarded-for", ""),
        "deploy_warnings": warnings,
    }
