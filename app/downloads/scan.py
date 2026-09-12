"""Folder-scan reconciliation: finds files already on disk (downloaded before
this feature existed, or placed there manually) and marks them as completed
Downloads, without re-downloading or moving anything. Matching is by filename
alone (case-insensitive), not full predicted path — a file already relocated
to a type-specific destination folder sits flat there, no longer matching
paths.predict_download_path()'s nested <bundle>/<item>/ staging shape, so
filename is the only join key that works uniformly across both an untouched
staging folder and an already-organized destination folder.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from app.connectors.humble_connector import parse_bundle
from app.downloads.paths import humanize_filename
from app.models.bundle import Bundle
from app.models.download import STATUS_COMPLETED, Download


def build_expected_index(db: Session) -> dict[str, list[dict]]:
    """filename.lower() -> every (bundle, item) that could have produced a file
    with that name. A list, not a single entry, because two different items
    can coincidentally share a filename — see ScanResult.ambiguous below.
    """
    index: dict[str, list[dict]] = {}
    for bundle in db.query(Bundle).all():
        normalized = parse_bundle(bundle.gamekey, json.loads(bundle.raw_json))
        for item in normalized.downloads:
            entry = {
                "gamekey": bundle.gamekey,
                "bundle_name": bundle.name,
                "item_name": item.item_name,
                "machine_name": item.machine_name,
                "subproduct_index": item.subproduct_index,
                "file_format": item.file_format,
                "source_url": item.source_url,
                "expected_size_bytes": item.expected_size_bytes,
                "original_filename": item.original_filename,
            }
            index.setdefault(item.original_filename.lower(), []).append(entry)
    return index


@dataclass
class ScanResult:
    matched: list[dict] = field(default_factory=list)
    ambiguous: list[dict] = field(default_factory=list)
    unmatched_count: int = 0
    already_downloaded_count: int = 0


def build_completed_keys(db: Session) -> set[tuple[str, str, str]]:
    """(gamekey, item_name, original_filename) for every already-completed
    Download row. scan_folder excludes these from `matched` so a file
    already tracked — from a real download, or committed by an earlier scan
    — doesn't keep occupying a slot in the (preview-limited) matched list.
    Without this, committing the first page of a large scan would never
    make room for the next page: the same already-tracked entries would
    just keep re-appearing at the front on every re-scan.
    """
    return {
        (gamekey, item_name, original_filename)
        for gamekey, item_name, original_filename in db.query(
            Download.gamekey, Download.item_name, Download.original_filename
        ).filter(Download.status == STATUS_COMPLETED)
    }


def scan_folder(
    root: Path,
    index: dict[str, list[dict]],
    completed_keys: set[tuple[str, str, str]] = frozenset(),
) -> ScanResult:
    # root is already confirmed contained within settings.scan_root_dir by
    # the router's _resolve_scan_folder() before this is ever called.
    result = ScanResult()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        candidates = index.get(path.name.lower())
        if not candidates:
            result.unmatched_count += 1
        elif len(candidates) == 1:
            entry = dict(candidates[0])
            key = (entry["gamekey"], entry["item_name"], entry["original_filename"])
            if key in completed_keys:
                result.already_downloaded_count += 1
                continue
            entry["found_path"] = str(path)
            entry["actual_size_bytes"] = path.stat().st_size
            expected = entry["expected_size_bytes"]
            # Informational only, never disqualifying — worker.py's own real-account
            # findings established Humble's reported file_size can be stale in either
            # direction, so a mismatch here doesn't mean the match is wrong.
            entry["size_mismatch"] = bool(expected) and expected != entry["actual_size_bytes"]
            result.matched.append(entry)
        else:
            result.ambiguous.append({"filename": path.name, "found_path": str(path), "candidates": candidates})
    return result


def _humanize_found_path(found_path: str) -> str:
    """Same underscore->space rename applied to a fresh download (see
    paths.humanize_filename) — a file the scan found on disk deserves the
    same readable name, so this only runs at commit time (never during
    preview, which must never touch the filesystem). Best-effort: a rename
    failure (e.g. a same-named file already sitting next to it) just leaves
    the file at its original name rather than failing the whole commit.
    """
    path = Path(found_path)
    humanized_name = humanize_filename(path.name)
    if humanized_name == path.name:
        return found_path
    target = path.with_name(humanized_name)
    try:
        path.rename(target)
    except OSError:
        return found_path
    return str(target)


def commit_matches(db: Session, result: ScanResult) -> int:
    """Upserts a completed Download row per matched entry, keyed the same way
    worker.py's _row_for() already keys live downloads: (gamekey, item_name,
    original_filename). Skips a row that's already STATUS_COMPLETED rather
    than overwriting it — that row is already tracked from a real download
    (and possibly already relocated); a stray duplicate the scan happens to
    find elsewhere shouldn't clobber its real, known-good location. This also
    makes committing the same scan twice a safe no-op, never a duplicate row.

    completed_at is deliberately left unset — this item's real completion
    time was never observed by the app, and stamping "now" would misrepresent
    history that doesn't exist.

    Also renames the file on disk (see _humanize_found_path) — a file the
    scan is about to start tracking gets the same underscore->space
    readability fix a fresh download gets, but only for entries actually
    being committed here, never for the full unfiltered scan result.
    """
    # One bulk fetch (bounded by the distinct bundles matched, not the file
    # count) instead of a query per matched entry — a first-time scan of an
    # existing library can match hundreds of files at once.
    gamekeys = {entry["gamekey"] for entry in result.matched}
    existing = {
        (d.gamekey, d.item_name, d.original_filename): d
        for d in db.query(Download).filter(Download.gamekey.in_(gamekeys)).all()
    }

    committed = 0
    for entry in result.matched:
        key = (entry["gamekey"], entry["item_name"], entry["original_filename"])
        row = existing.get(key)
        if row is not None and row.status == STATUS_COMPLETED:
            continue
        entry["found_path"] = _humanize_found_path(entry["found_path"])
        if row is None:
            row = Download(
                gamekey=entry["gamekey"],
                item_name=entry["item_name"],
                original_filename=entry["original_filename"],
            )
            db.add(row)
            existing[key] = row
        row.bundle_name = entry["bundle_name"]
        row.subproduct_index = entry["subproduct_index"]
        row.file_format = entry["file_format"]
        row.source_url = entry["source_url"]
        row.status = STATUS_COMPLETED
        row.error_message = ""
        row.expected_size_bytes = entry["actual_size_bytes"]
        row.progress_bytes = entry["actual_size_bytes"]
        row.current_location_type = "local"
        row.current_location_path = entry["found_path"]
        committed += 1
    db.commit()
    return committed
