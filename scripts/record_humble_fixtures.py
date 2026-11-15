"""Re-record the Humble storefront fixtures the contract tests parse.

    python scripts/record_humble_fixtures.py

Fetches the live /bundles listing and one books + one games bundle page,
trims each to a handful of REAL records (real keys, real nesting, real
escaping — nothing invented), and writes them to tests/fixtures/humble/.
Run it when Humble changes its markup: the contract tests
(tests/test_storefront_contract.py) then fail against the old fixtures until
the parser is fixed, and pass against the new ones.

Read-only: three public GETs, no login.
"""

import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.connectors import storefront  # noqa: E402

OUT = ROOT / "tests" / "fixtures" / "humble"
COMPACT = (",", ":")


def _page(chunk: str, title: str) -> str:
    """The smallest HTML that carries `chunk` the way Next.js does."""
    return (
        f"<!DOCTYPE html><html><head><title>{title}</title></head><body>"
        f"<script>self.__next_f.push([1,{json.dumps(chunk)}])</script></body></html>\n"
    )


def _get(client: httpx.Client, url: str) -> str:
    resp = client.get(url)
    resp.raise_for_status()
    return resp.text


def record_listing(html: str) -> tuple[str, dict[str, str]]:
    """A listing trimmed to the first bundle of each category; also returns
    each category's bundle url for the detail recordings."""
    hits = [h for h in storefront._objects_after(storefront._flight_text(html), '{"relationTo":"algolia-hit","value":')]
    chosen: dict[str, dict] = {}
    for hit in hits:
        if hit.get("productType") == "humble-bundle":
            chosen.setdefault(hit["category"], hit)
    if set(chosen) < {"books", "games", "software"}:
        raise SystemExit(f"listing no longer yields all three categories: {sorted(chosen)}")
    chunk = "5:" + json.dumps(
        [{"relationTo": "algolia-hit", "value": h} for h in chosen.values()], separators=COMPACT
    )
    return _page(chunk, "All Bundles"), {cat: h["url"] for cat, h in chosen.items()}


def record_detail(html: str, title: str, items_per_tier: int = 2) -> str:
    stream = storefront._flight_text(html)
    page = next(
        p
        for p in storefront._objects_after(stream, '"bundlePage":')
        if isinstance(p, dict) and isinstance(p.get("tiers"), list)
    )
    # Keep the structure the parser reads; drop the bulk it never touches.
    for heavy in ("meta", "supportingCharities", "paymentSplit", "charityHeavyPaymentSplit", "bundleProductFeed"):
        page.pop(heavy, None)
    for tier in page["tiers"]:
        kept = []
        for entry in tier.get("bundleItems", []):
            if isinstance(entry, dict):
                entry.pop("carousel", None)
                entry.pop("previewPdf", None)
            kept.append(entry)
        # Keep back-references (strings) and the first few real items per tier.
        tier["bundleItems"] = [e for e in kept if isinstance(e, str)] + [e for e in kept if isinstance(e, dict)][:items_per_tier]
    page_json = json.dumps(page, separators=COMPACT)
    chunk = 'e:[["$","section",null,{"children":["$","$L6e",null,{"bundlePage":' + page_json + "}]}]]"
    # Long strings (game descriptions) live in their own text rows, referenced
    # as "$<id>"; keep the rows the kept records still point at, byte-length
    # prefixed exactly as the site sends them.
    for row_id, text in storefront._text_rows(stream).items():
        if f'"${row_id}"' in page_json:
            chunk += f"\n{row_id}:T{len(text.encode('utf-8')):x},{text}"
    return _page(chunk, title)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with httpx.Client(headers={"User-Agent": storefront.USER_AGENT}, follow_redirects=True, timeout=30) as client:
        listing_html, urls = record_listing(_get(client, f"{storefront.BASE_URL}/bundles"))
        (OUT / "listing.html").write_text(listing_html, encoding="utf8")
        for category in ("books", "games"):
            html = record_detail(_get(client, f"{storefront.BASE_URL}{urls[category]}"), f"{category} bundle")
            (OUT / f"detail_{category}.html").write_text(html, encoding="utf8")
    for f in sorted(OUT.iterdir()):
        print(f"{f.name}: {f.stat().st_size:,} bytes")
    print("Fixtures recorded. Review `git diff tests/fixtures` before committing.")


if __name__ == "__main__":
    main()
