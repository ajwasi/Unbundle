"""One-off manual check for whether app/connectors/chirp_connector.py's plain
httpx login actually works against the real chirpbooks.com — the single
biggest open unknown in that connector (see its own module docstring):
Chirp sits behind Cloudflare, and this app already has a real precedent
(audible_connector.py's own history) for a headless HTTP client being
structurally unable to pass a JS challenge that a real browser sails through
without noticing.

Run this from wherever the answer actually needs to hold — ideally the same
network/environment this app would really be deployed on, not a one-off dev
sandbox — since Cloudflare's bot scoring can reasonably differ by network
reputation. Never commit real credentials; this script only ever reads them
from environment variables or an interactive prompt.

Usage:
    CHIRP_EMAIL=you@example.com CHIRP_PASSWORD='...' python scripts/verify_chirp_login.py
    (or just run it with no env vars set and it will prompt instead)

Safe to re-run. Makes real network calls to chirpbooks.com and nothing else;
never writes to this app's own database.
"""

import asyncio
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx

from app.connectors import chirp_connector as chirp


async def main() -> int:
    email = os.environ.get("CHIRP_EMAIL") or input("Chirp email: ")
    password = os.environ.get("CHIRP_PASSWORD") or getpass.getpass("Chirp password: ")

    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Gecko/20100101 Firefox/156.0"}
    ) as client:
        print("Logging in...")
        try:
            await chirp.login(client, email, password)
        except chirp.ChirpAuthError as exc:
            print(f"LOGIN FAILED: {exc}")
            print(
                "If this looks like it came back almost instantly with a generic error, "
                "or the response text mentions Cloudflare/'checking your browser'/a challenge "
                "page rather than Chirp's own login form, that's the Cloudflare blocker this "
                "script exists to detect — see chirp_connector.py's module docstring for the "
                "fallback plan (reuse a browser-obtained session instead of logging in here)."
            )
            return 1

        print("Login appeared to succeed (no redirect back to sign-in, no rejection text).")

        print("Fetching page 1 of the library...")
        try:
            books, total = await chirp.fetch_library_page(client, page=1, per_page=20)
        except chirp.ChirpRequestError as exc:
            print(f"LOGIN worked, but the library query failed: {exc}")
            print(
                "This means the *reconstructed* GraphQL query (operationName/pagination args "
                "guessed from a response shape, not a captured request) needs correcting — "
                "capture the real request from DevTools and update _LIBRARY_QUERY in "
                "chirp_connector.py to match."
            )
            return 1

        print(f"Library query succeeded: {len(books)} books on this page, {total} total.")
        for book in books[:5]:
            print(f"  - {book.title} ({book.authors}) [audiobook_id={book.audiobook_id}]")
        if total > len(books):
            print(f"  ... and {total - len(books)} more (pagination confirmed working if this matches your real count).")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
