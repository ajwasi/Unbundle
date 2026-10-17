"""Folder-scan reconciliation for Audible audiobooks: finds .m4b/.m4a files
already on disk (downloaded before this feature existed, or placed there
manually) and records them in AudibleLocalFile, so the downloader skips them.
Nothing is moved, renamed or re-downloaded.

Matches a file's name (minus extension) to a book's title, ignoring case and
anything that isn't a letter or digit — so "Dune: Part One.m4b", "Dune  Part
One.m4b" (what this app's own downloader writes, see
paths.sanitize_dir_name) and "dune_part_one.m4b" all find the same book. Two
books whose titles normalize identically are reported as ambiguous and never
committed automatically, the same rule the Humble scan applies.
"""

from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from app.audible.book_downloader import record_local_file
from app.models.audible_book import AudibleBook
from app.models.audible_local_file import SOURCE_SCAN, AudibleLocalFile

AUDIO_SUFFIXES = {".m4b", ".m4a"}


def normalize(text: str) -> str:
    return "".join(ch for ch in text.casefold() if ch.isalnum())


@dataclass
class ScanResult:
    matched: list[dict] = field(default_factory=list)
    ambiguous: list[dict] = field(default_factory=list)
    unmatched_count: int = 0
    already_tracked_count: int = 0


def build_title_index(db: Session) -> dict[str, list[dict]]:
    index: dict[str, list[dict]] = {}
    for asin, title, author in db.query(AudibleBook.asin, AudibleBook.title, AudibleBook.author):
        key = normalize(title)
        if key:
            index.setdefault(key, []).append({"asin": asin, "title": title, "author": author})
    return index


def scan_folder(root: Path, index: dict[str, list[dict]], tracked: dict[str, str]) -> ScanResult:
    """`tracked` is asin -> recorded path for books already in
    AudibleLocalFile; a file matching one of those at the same path is
    counted, not listed again, so committing a page of a big scan makes room
    for the next (same reasoning as downloads/scan.py's completed_keys).
    """
    # root is already confirmed contained within a configured scan root by
    # the router's _resolve_scan_folder() before this is ever called.
    result = ScanResult()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in AUDIO_SUFFIXES:
            continue
        candidates = index.get(normalize(path.stem))
        if not candidates:
            result.unmatched_count += 1
        elif len(candidates) == 1:
            entry = dict(candidates[0])
            if tracked.get(entry["asin"]) == str(path):
                result.already_tracked_count += 1
                continue
            entry["found_path"] = str(path)
            entry["size_bytes"] = path.stat().st_size
            result.matched.append(entry)
        else:
            result.ambiguous.append({"filename": path.name, "found_path": str(path), "candidates": candidates})
    return result


def tracked_paths(db: Session) -> dict[str, str]:
    return {asin: path for asin, path in db.query(AudibleLocalFile.asin, AudibleLocalFile.path)}


def commit_matches(db: Session, result: ScanResult) -> int:
    """Records each matched file. A book matched by several files (a duplicate
    copy somewhere) keeps the first one found; re-committing the same scan is
    a no-op. A book already recorded at a path that still exists is left
    alone — that record was either observed from a real download or chosen
    by an earlier scan, and a stray duplicate shouldn't displace it.
    """
    committed = 0
    seen: set[str] = set()
    for entry in result.matched:
        asin = entry["asin"]
        if asin in seen:
            continue
        seen.add(asin)
        existing = db.get(AudibleLocalFile, asin)
        if existing is not None and Path(existing.path).is_file():
            continue
        record_local_file(db, asin, Path(entry["found_path"]), SOURCE_SCAN)
        committed += 1
    return committed
