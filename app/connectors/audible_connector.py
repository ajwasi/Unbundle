"""Audible — like steam_connector.py/gog_connector.py, a library-listing
connector with no BaseConnector interface and no entitlement matching
(Audible purchases never come through a Humble bundle, so there's nothing
here to cross-reference). Wraps the `audible` package
(https://audible.readthedocs.io, PyPI: audible==0.12.0), which reimplements
Amazon's private device-registration API — there is no public Audible API.

**Login is external-browser + paste-URL, the same shape as GOG's
(gog_connector.py's LOGIN_URL + extract_code() + a paste-back form) — not
because this app chose that UX, but because it's the only one that still
works.** An earlier version of this connector submitted the user's Amazon
username/password itself (audible.Authenticator.from_login(), with inline
CAPTCHA/OTP/CVF/approval callbacks bridged through a background thread —
see git history on this file for that whole implementation and its
diagnostic trail). Real-account testing eventually traced every remaining
failure to one root cause: Amazon now puts an AWS WAF JavaScript challenge
in front of this login path, and a plain HTTP client cannot solve it — the
"CVF" page audible's login() thought it was showing was actually the WAF
challenge's non-JS fallback ("JavaScript Is Disabled ... something went
wrong"). This is a known, upstream-acknowledged dead end, not something
fixable here: see mkb79/Audible#732 ("New Amazon WAF CAPTCHA Prompt During
Login"), closed *not planned* — a headless HTTP client cannot pass a JS
challenge, full stop.

`audible` ships a second login mode built for exactly this:
Authenticator.from_login_external(locale, login_url_callback). Instead of
this app ever touching Amazon credentials, it hands the user a real Amazon
OAuth URL; they open it in their own actual browser (which passes the WAF
challenge the same way any normal Amazon browsing does), log in, land on a
mostly-blank Amazon page whose URL contains
`openid.oa2.authorization_code=...`, and paste that URL back here.

from_login_external() is still a **synchronous** call — login_url_callback
blocks until it returns, exactly like the old captcha/otp/cvf/approval
callbacks did — so this module keeps the same background-thread +
queue.Queue() bridge as before (single pending login at a time,
module-level state), just with one prompt instead of four.
"""

import queue
import threading
from dataclasses import dataclass
from datetime import datetime

import audible
import httpx
import nh3

# Amazon's own Plus Catalog access marker — the one value confirmed from
# community reverse-engineering references (not yet confirmed against this
# app's own real account). Unknown/empty benefit_id defaults to "owned"
# deliberately: failing toward showing a real purchase as owned is far less
# harmful than mislabeling one as a rental, and this is a single, isolated
# check to correct once a real account's actual value set is known.
_PLUS_CATALOG_BENEFIT_IDS = {"AYCL"}

# Confirmed via audible/auth.py: from_login_external() calls the same
# shared register_() device-registration function from_login() did (a real
# POST to .../auth/register, plus bare httpx.post()/httpx.get() calls in
# audible/auth.py — neither goes through httpx.Client), which is what
# actually needed this patch before. Nothing here calls audible.login's
# CVF/CAPTCHA page-parsing code anymore, but the registration step after a
# successful paste-back is unchanged, so this timeout patch still applies.
# Restored in a finally block regardless of outcome. Confirmed safe to patch
# globally: this app's every other connector uses httpx.AsyncClient
# exclusively — nothing else here ever constructs a sync httpx.Client or
# calls the bare httpx.post/httpx.get module functions.
_LOGIN_HTTP_TIMEOUT_SECONDS = 60.0

_pending_lock = threading.Lock()
_pending: dict | None = None


class AudibleLoginError(Exception):
    """Raised by start_login when a login attempt is already in progress."""


@dataclass
class AudiblePendingPrompt:
    login_url: str  # the Amazon OAuth URL to open in a real browser


@dataclass
class AudibleBookData:
    asin: str
    title: str
    author: str
    runtime_minutes: int
    cover_url: str
    purchase_date: datetime | None = None
    price_amount: float | None = None
    price_currency: str = ""
    series_title: str = ""
    series_sequence: str = ""
    rating_average: float | None = None
    description: str = ""
    is_finished: bool = False
    percent_complete: float = 0
    pdf_url: str = ""
    benefit_id: str = ""


def is_owned(benefit_id: str) -> bool:
    """False only for a known Plus-Catalog-style benefit_id — see
    _PLUS_CATALOG_BENEFIT_IDS above for why unknown/empty defaults True.
    """
    return benefit_id not in _PLUS_CATALOG_BENEFIT_IDS


def _parse_purchase_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_price(item: dict) -> tuple[float | None, str]:
    # Confirmed against a real account (2026-09-14): the library endpoint's
    # "price" key is always null for every already-owned item, regardless of
    # which response_groups are requested — Amazon only populates it for
    # something you don't yet own (i.e. still shopping), not a purchase
    # history. order_id/order_item_id are also always null here, so there is
    # no "amount actually paid" data anywhere in this response at all. This
    # is kept only for the rare edge case (if any) where it's non-null —
    # expect price_amount to be None for virtually everything.
    try:
        list_price = (item.get("price") or {}).get("list_price") or {}
        amount = list_price.get("base")
        return (float(amount) if amount is not None else None, str(list_price.get("currency_code") or ""))
    except (TypeError, ValueError, AttributeError):
        return (None, "")


def _parse_series(item: dict) -> tuple[str, str]:
    try:
        series = (item.get("series") or [])[0]
        return (str(series.get("title") or ""), str(series.get("sequence") or ""))
    except (TypeError, ValueError, IndexError, AttributeError):
        return ("", "")


def _parse_rating(item: dict) -> float | None:
    # Exact shape unconfirmed against a real account — best-effort walk,
    # never raises.
    try:
        overall = (item.get("rating") or {}).get("overall_distribution") or {}
        value = overall.get("display_average_rating")
        return float(value) if value is not None else None
    except (TypeError, ValueError, AttributeError):
        return None


def login_status() -> AudiblePendingPrompt | None:
    """The login URL waiting for a pasted-back redirect, if a login attempt
    is mid-flight — None if nothing is in progress, or the attempt already
    finished (see login_result)."""
    with _pending_lock:
        if _pending is None or _pending["done"]:
            return None
        return AudiblePendingPrompt(login_url=_pending["login_url"])


def login_result() -> tuple[audible.Authenticator | None, str | None] | None:
    """None while still in progress (or nothing running). Once done:
    (authenticator, None) on success, (None, error_message) on failure.
    """
    with _pending_lock:
        if _pending is None or not _pending["done"]:
            return None
        return (_pending["result"], _pending["error"])


def clear_pending() -> None:
    """Call once login_result() has been consumed (saved or shown as an
    error) — leaves this module ready for a fresh attempt."""
    global _pending
    with _pending_lock:
        _pending = None


def start_login(locale: str) -> None:
    """Starts a login attempt on a background thread and returns
    immediately — poll login_status()/login_result() to see how it's going
    (routers/settings.py polls the same way bundles/_refresh_status.html
    already polls a running download job).
    """
    global _pending
    with _pending_lock:
        if _pending is not None and not _pending["done"]:
            raise AudibleLoginError("A login attempt is already in progress.")
        _pending = {
            "login_url": "",
            "answer_queue": queue.Queue(),
            "result": None,
            "error": None,
            "done": False,
        }
    state = _pending

    def _login_url_callback(oauth_url: str) -> str:
        with _pending_lock:
            state["login_url"] = oauth_url
        return state["answer_queue"].get()  # blocks until answer_login() is called

    def _run() -> None:
        original_client_cls = httpx.Client
        original_post = httpx.post
        original_get = httpx.get

        class _TimeoutClient(httpx.Client):
            def __init__(self, *args, **kwargs):
                kwargs.setdefault("timeout", _LOGIN_HTTP_TIMEOUT_SECONDS)
                super().__init__(*args, **kwargs)

        def _post_with_timeout(*args, **kwargs):
            kwargs.setdefault("timeout", _LOGIN_HTTP_TIMEOUT_SECONDS)
            return original_post(*args, **kwargs)

        def _get_with_timeout(*args, **kwargs):
            kwargs.setdefault("timeout", _LOGIN_HTTP_TIMEOUT_SECONDS)
            return original_get(*args, **kwargs)

        httpx.Client = _TimeoutClient
        httpx.post = _post_with_timeout
        httpx.get = _get_with_timeout
        try:
            auth = audible.Authenticator.from_login_external(
                locale,
                login_url_callback=_login_url_callback,
            )
            with _pending_lock:
                state["result"] = auth
                state["done"] = True
        except Exception as exc:  # noqa: BLE001 - surfaced as a login error, never a crash
            with _pending_lock:
                state["error"] = str(exc)
                state["done"] = True
        finally:
            httpx.Client = original_client_cls
            httpx.post = original_post
            httpx.get = original_get

    threading.Thread(target=_run, daemon=True).start()


def answer_login(pasted_url: str) -> None:
    """Submits the URL the user was redirected to after logging in on
    Amazon's own site, unblocking the waiting login_url_callback. A no-op if
    nothing is pending."""
    with _pending_lock:
        if _pending is None:
            return
        _pending["answer_queue"].put(pasted_url)


async def fetch_library(auth: audible.Authenticator) -> list[AudibleBookData]:
    """One owned-titles page (num_results=1000 — comfortably above any real
    personal library; audible-cli itself paginates for very large libraries,
    not attempted here). response_groups mirrors audible-cli's own default
    request shape for the fields this app actually uses (title/author/
    runtime/cover).
    """
    async with audible.AsyncClient(auth) as client:
        resp = await client.get(
            "library",
            params={
                # Best-effort superset — Amazon silently ignores response_groups
                # it doesn't recognize, confirmed safe to over-ask. series/rating
                # are confirmed populated against a real account; "price" is
                # confirmed to always come back null for already-owned items
                # (see _parse_price's own comment) but kept in the request in
                # case that's ever not true for some edge case (a gift not yet
                # redeemed, etc). benefit_id (via product_attrs) is present but
                # every sample seen so far was null too — is_owned() defaults to
                # True for that reason; still worth another look if a real Plus
                # Catalog title ever shows up in a synced library to confirm the
                # actual non-owned value.
                "response_groups": (
                    "product_desc,media,contributors,price,series,rating,"
                    "is_finished,percent_complete,pdf_url,product_attrs"
                ),
                "num_results": 1000,
            },
        )

    books: list[AudibleBookData] = []
    for item in resp.get("items") or []:
        asin = item.get("asin")
        if not asin:
            continue
        authors = ", ".join(a["name"] for a in item.get("authors") or [] if a.get("name"))
        images = item.get("product_images") or {}
        cover = images.get("500") or next(iter(images.values()), "")
        price_amount, price_currency = _parse_price(item)
        series_title, series_sequence = _parse_series(item)
        description = nh3.clean(item.get("publisher_summary") or item.get("merchandising_summary") or "", tags=set())
        books.append(
            AudibleBookData(
                asin=asin,
                title=item.get("title") or asin,
                author=authors,
                runtime_minutes=int(item.get("runtime_length_min") or 0),
                cover_url=cover,
                purchase_date=_parse_purchase_date(item.get("purchase_date")),
                price_amount=price_amount,
                price_currency=price_currency,
                series_title=series_title,
                series_sequence=series_sequence,
                rating_average=_parse_rating(item),
                description=description,
                is_finished=bool(item.get("is_finished")),
                percent_complete=float(item.get("percent_complete") or 0),
                pdf_url=item.get("pdf_url") or "",
                benefit_id=str(item.get("benefit_id") or ""),
            )
        )
    return books
