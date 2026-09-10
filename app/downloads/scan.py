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


def scan_folder(root: Path, index: dict[str, list[dict]]) -> ScanResult:
    # root is already confirmed contained within settings.scan_root_dir by
    # the router's _resolve_scan_folder() (paths.resolve_within()) before
    # this is ever called.
    result = ScanResult()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        candidates = index.get(path.name.lower())
        if not candidates:
            result.unmatched_count += 1
        elif len(candidates) == 1:
            entry = dict(candidates[0])
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
    """
    committed = 0
    for entry in result.matched:
        row = (
            db.query(Download)
            .filter(
                Download.gamekey == entry["gamekey"],
                Download.item_name == entry["item_name"],
                Download.original_filename == entry["original_filename"],
            )
            .one_or_none()
        )
        if row is not None and row.status == STATUS_COMPLETED:
            continue
        if row is None:
            row = Download(
                gamekey=entry["gamekey"],
                item_name=entry["item_name"],
                original_filename=entry["original_filename"],
            )
            db.add(row)
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
