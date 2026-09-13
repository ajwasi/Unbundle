"""Audible — like steam_connector.py/gog_connector.py, a library-listing
connector with no BaseConnector interface and no entitlement matching
(Audible purchases never come through a Humble bundle, so there's nothing
here to cross-reference). Wraps the `audible` package
(https://audible.readthedocs.io, PyPI: audible==0.12.0), which reimplements
Amazon's private device-registration API — there is no public Audible API.

Every method signature referenced below was confirmed directly against the
installed package via introspection (inspect.signature / reading
audible/auth.py and audible/client.py), not just its docs.

The login flow is a genuinely different shape from every other connector
here: Authenticator.from_login(...) is a **synchronous, blocking** call —
whenever Amazon demands one of four possible extra verification steps
(CAPTCHA, a 2FA/OTP code, a CVF code sent by mail/SMS, or an "approval
alert" push notification the user must acknowledge elsewhere — confirmed by
reading audible/login.py's own from_login implementation, which checks for
all four independently), it invokes the corresponding callback *inline* and
blocks until that callback returns. All four must be supplied — leaving any
one as None makes audible fall back to its own console-based default
(builtin input()), which raises a bare "EOF when reading a line" the moment
it runs with no attached terminal, i.e. always, inside this container
(confirmed live: an early version of this file only wired up captcha/otp and
hit exactly this on a real account whose login happened to need a CVF
step). An HTTP request/response cycle can't pause mid-request waiting for a
second request from the browser either way, so this module bridges the gap
with a background thread (from_login does real blocking network I/O plus
the inline callback waits below, so it must never run on the event loop)
and a queue.Queue() the pending callback blocks on until
routers/settings.py's answer-submission route calls answer_login(). Single
pending login at a time, module-level state — the same "no multi-tenant
complexity" assumption already used throughout this single-user app (e.g.
ratelimit.py's per-process counters).

A real account also hit "The read operation timed out". Two rounds of this:
first traced to audible/login.py's own httpx.Client() (no timeout override,
so httpx's tight 5-second default applies) — patched, but the *same* error
persisted at the *same* point against the same real account. Root cause
actually goes one level deeper: once the interactive captcha/otp/cvf/approval
part succeeds, from_login() moves on to registering a "device" with Amazon
(audible/register.py's own POST to .../auth/register, plus a couple of bare
calls in audible/auth.py) — and those don't go through httpx.Client at all,
they call the bare module-level httpx.post()/httpx.get() convenience
functions, which patching httpx.Client does nothing to. Both httpx.Client
*and* httpx.post/httpx.get are patched now, for the same reason and the same
duration — see _LOGIN_HTTP_TIMEOUT_SECONDS below, restored in a finally
block regardless of outcome, exactly like httpx.Client.

Separately, the same account reaches the CVF prompt but never receives a
code by email or SMS. cvf_callback() takes zero arguments (confirmed via
inspect.signature), so there's no supported way to see what Amazon actually
told the user at that point through the callback itself. start_login()'s
_run() temporarily wraps audible.login.check_for_cvf() (a pure boolean check
login() already calls on every page it fetches) to log the cvf page's own
text to stderr the moment the prompt first fires — a real diagnostic, not a
guess, without changing what that function returns or otherwise touching
the real login flow. Remove this once the underlying cause is understood.
"""

import queue
import sys
import threading
from dataclasses import dataclass

import audible
import httpx

# Neither audible/login.py's httpx.Client() nor audible/register.py's and
# auth.py's bare httpx.post()/httpx.get() calls override httpx's own tight
# default timeout (5 seconds per connect/read/write) — and from_login()
# exposes no way to pass a custom one through any of them (confirmed via
# inspect.signature — no timeout param exists anywhere in the public API).
# The only way to give a real, possibly-slower homelab network path enough
# time is to patch all three in underneath it. Confirmed safe to patch
# globally (not scoped to a specific audible submodule) rather than
# something narrower: this app's every other connector uses
# httpx.AsyncClient exclusively — nothing else here ever constructs a sync
# httpx.Client or calls the bare httpx.post/httpx.get module functions — so
# this can't affect unrelated in-flight requests, and all three are restored
# immediately after from_login() returns either way (see start_login's
# _run()).
_LOGIN_HTTP_TIMEOUT_SECONDS = 60.0

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

    def _cvf_callback() -> str:
        return _wait_for_answer("cvf", "")

    def _approval_callback():
        # Amazon's "approval alert" flow: a push notification/email asking
        # the user to approve the login elsewhere, no code to type in — the
        # return value is never actually used (confirmed reading
        # audible/login.py, which just calls this and moves on), so any
        # answer at all unblocks it. Real content is only ever "captcha",
        # "otp", or "cvf" — "approval" needs no input(), just an
        # acknowledgment.
        return _wait_for_answer("approval", "")

    def _run() -> None:
        original_client_cls = httpx.Client
        original_post = httpx.post
        original_get = httpx.get
        original_check_for_cvf = audible.login.check_for_cvf

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

        def _check_for_cvf_with_logging(soup):
            # Temporary diagnostic: a real account reaches this prompt but
            # never receives a code by email or SMS. cvf_callback() itself
            # takes no arguments (confirmed via inspect.signature — audible
            # gives it zero context), so there is no way to see what Amazon
            # actually told the user through the supported callback API at
            # all. This spies on check_for_cvf() instead (a pure boolean
            # check the real login() already calls on every page it fetches)
            # to log the cvf-page-content div's own text — whatever Amazon
            # says there (which channel, a resend link, an error) the moment
            # this prompt first fires, without changing what it returns or
            # otherwise touching the real login flow.
            result = original_check_for_cvf(soup)
            if result:
                try:
                    content = soup.find("div", id="cvf-page-content")
                    text = content.get_text(" ", strip=True) if content else "(no cvf-page-content div found)"
                except Exception as exc:  # noqa: BLE001 - diagnostics must never break the real login
                    text = f"(failed to extract diagnostic text: {exc})"
                print(f"AUDIBLE LOGIN DIAGNOSTIC (cvf page text): {text}", file=sys.stderr)
            return result

        httpx.Client = _TimeoutClient
        httpx.post = _post_with_timeout
        httpx.get = _get_with_timeout
        audible.login.check_for_cvf = _check_for_cvf_with_logging
        try:
            auth = audible.Authenticator.from_login(
                username,
                password,
                locale,
                captcha_callback=_captcha_callback,
                otp_callback=_otp_callback,
                cvf_callback=_cvf_callback,
                approval_callback=_approval_callback,
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
            audible.login.check_for_cvf = original_check_for_cvf

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
