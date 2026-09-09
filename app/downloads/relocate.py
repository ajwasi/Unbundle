"""Resolves and performs the post-download move from staging (downloads_dir)
to a configured DownloadDestination, per the precedence documented on that
model. humble-cli has no per-item output-directory flag (see paths.py), so
type/tag-based routing can only ever be a move-after-the-fact, never a
download-time redirect.
"""

import shutil
from pathlib import Path

from sqlalchemy.orm import Session

from app.models.download_destination import DownloadDestination
from app.models.tag import BundleTag, ItemTag


def _applicable_tag_ids(db: Session, gamekey: str, machine_name: str) -> set[int]:
    """A download's tags are the union of its bundle's tags and its own
    item-level tag (when it has a machine_name) — mirrors how the detail page
    already surfaces both (_bundle_tags/_item_tags), just merged into one set
    since routing doesn't care which level a tag came from.
    """
    tag_ids = {tid for (tid,) in db.query(BundleTag.tag_id).filter(BundleTag.gamekey == gamekey).all()}
    if machine_name:
        tag_ids |= {tid for (tid,) in db.query(ItemTag.tag_id).filter(ItemTag.machine_name == machine_name).all()}
    return tag_ids


def _formats_match(formats_field: str, file_format: str) -> bool:
    allowed = formats_field.strip()
    if not allowed:
        return True
    values = {f.strip().lower() for f in allowed.split(",") if f.strip()}
    return (file_format or "").strip().lower() in values


def resolve_destination(db: Session, gamekey: str, machine_name: str, file_format: str) -> Path | None:
    """Returns the destination directory a completed download should be moved
    to, or None if it should stay at its staging path. Precedence: tag+format
    match, then format-only, then the tag_id-NULL/formats-empty catch-all
    default. Ties within a tier (e.g. two tag rules both apply) resolve by
    lowest id — first-created wins, a simple deterministic rule rather than
    depending on incidental query order.
    """
    tag_ids = _applicable_tag_ids(db, gamekey, machine_name)
    rules = db.query(DownloadDestination).order_by(DownloadDestination.id).all()

    for rule in rules:
        if rule.tag_id is not None and rule.tag_id in tag_ids and _formats_match(rule.formats, file_format):
            return Path(rule.path)
    for rule in rules:
        if rule.tag_id is None and rule.formats.strip() and _formats_match(rule.formats, file_format):
            return Path(rule.path)
    for rule in rules:
        if rule.tag_id is None and not rule.formats.strip():
            return Path(rule.path)
    return None


def relocate(src: Path, dest_dir: Path) -> Path:
    """Moves src into dest_dir (created if needed), returning the new path.
    shutil.move (not os.replace) because the destination is commonly a
    different filesystem (a mounted network drive) — os.replace requires the
    same filesystem, while shutil.move falls back to copy+delete when a plain
    rename isn't possible.

    Refuses to overwrite an existing file at the destination — two different
    items (different bundles, or even the same generic filename like
    "book.epub") can resolve to the same destination directory, and the
    Download model's own uniqueness is per-(gamekey, item_name, filename), not
    global. The caller (worker.py) treats this as a failed relocation and
    leaves the file at its staging path rather than silently losing one copy.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / src.name
    if dest_path.exists():
        raise FileExistsError(f"'{dest_path}' already exists")
    shutil.move(str(src), str(dest_path))
    return dest_path
