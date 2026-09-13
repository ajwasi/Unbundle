"""Audible — like steam_connector.py/gog_connector.py, a library-listing
connector with no BaseConnector interface and no entitlement matching
(Audible purchases never come through a Humble bundle, so there's nothing
here to cross-reference). Wraps the `audible` package
(https://audible.readthedocs.io, PyPI: audible==0.12.0), which reimplements
Amazon's private device-registration API — there is no public Audible API.

Every method signature referenced below was confirmed directly against the
installed package via introspection (inspect.signature / reading
audible/auth.py and audible/client.py), not just its docs — but a real
Amazon login (with a real CAPTCHA/2FA challenge) has never been exercised
against this code from this environment, so treat the login flow as
best-effort until proven against a real account.

The login flow is a genuinely different shape from every other connector
here: Authenticator.from_login(...) is a **synchronous, blocking** call —
when Amazon demands a CAPTCHA or a 2FA (OTP) code, it invokes the given
callback *inline* and blocks until that callback returns a string. An HTTP
request/response cycle can't pause mid-request waiting for a second request
from the browser, so this module bridges the gap with a background thread
(from_login does real blocking network I/O plus the inline callback waits
below, so it must never run on the event loop) and a queue.Queue() the
pending callback blocks on until routers/settings.py's answer-submission
route calls answer_login(). Single pending login at a time, module-level
state — the same "no multi-tenant complexity" assumption already used
throughout this single-user app (e.g. ratelimit.py's per-process counters).
"""

import queue
import threading
from dataclasses import dataclass

import audible

_pending_lock = threading.Lock()
_pending: dict | None = None


class AudibleLoginError(Exception):
    """Raised by start_login when a login attempt is already in progress."""


@dataclass
class AudiblePendingPrompt:
    kind: str  # "captcha" or "otp"
    prompt: str  # the CAPTCHA image URL for "captcha"; unused ("") for "otp"


@dataclass
class AudibleBookData:
    asin: str
    title: str
    author: str
    runtime_minutes: int
    cover_url: str


def login_status() -> AudiblePendingPrompt | None:
    """The currently pending CAPTCHA/OTP prompt, if a login attempt is
    mid-flight and waiting on one — None if nothing is in progress, or the
    attempt already finished (see login_result)."""
    with _pending_lock:
        if _pending is None or _pending["done"]:
            return None
        return AudiblePendingPrompt(kind=_pending["kind"], prompt=_pending["prompt"])


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


def start_login(username: str, password: str, locale: str) -> None:
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
            "kind": "",
            "prompt": "",
            "answer_queue": queue.Queue(),
            "result": None,
            "error": None,
            "done": False,
        }
    state = _pending

    def _wait_for_answer(kind: str, prompt: str) -> str:
        with _pending_lock:
            state["kind"] = kind
            state["prompt"] = prompt
        return state["answer_queue"].get()  # blocks until answer_login() is called

    def _captcha_callback(captcha_url: str) -> str:
        return _wait_for_answer("captcha", captcha_url)

    def _otp_callback() -> str:
        return _wait_for_answer("otp", "")

    def _run() -> None:
        try:
            auth = audible.Authenticator.from_login(
                username,
                password,
                locale,
                captcha_callback=_captcha_callback,
                otp_callback=_otp_callback,
            )
            with _pending_lock:
                state["result"] = auth
                state["done"] = True
        except Exception as exc:  # noqa: BLE001 - surfaced as a login error, never a crash
            with _pending_lock:
                state["error"] = str(exc)
                state["done"] = True

    threading.Thread(target=_run, daemon=True).start()


def answer_login(answer: str) -> None:
    """Submits the user's CAPTCHA/OTP answer, unblocking whichever callback
    is currently waiting on one. A no-op if nothing is pending."""
    with _pending_lock:
        if _pending is None:
            return
        _pending["answer_queue"].put(answer)


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
            params={"response_groups": "product_desc,media,contributors", "num_results": 1000},
        )

    books: list[AudibleBookData] = []
    for item in resp.get("items") or []:
        asin = item.get("asin")
        if not asin:
            continue
        authors = ", ".join(a["name"] for a in item.get("authors") or [] if a.get("name"))
        images = item.get("product_images") or {}
        cover = images.get("500") or next(iter(images.values()), "")
        books.append(
            AudibleBookData(
                asin=asin,
                title=item.get("title") or asin,
                author=authors,
                runtime_minutes=int(item.get("runtime_length_min") or 0),
                cover_url=cover,
            )
        )
    return books
