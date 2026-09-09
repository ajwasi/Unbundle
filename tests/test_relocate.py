from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.config import settings
from app.downloads import paths, relocate, worker
from app.models.download import STATUS_COMPLETED, Download
from app.models.download_destination import DownloadDestination
from app.models.download_job import DownloadJob
from app.models.tag import BundleTag, ItemTag, Tag
from tests.factories import make_order, make_subproduct


def _predicted_path(bundle_name, item_name, filename):
    return settings.downloads_dir / paths.predict_download_path(bundle_name, item_name, filename)


# --- resolve_destination() precedence ---------------------------------------


def test_resolve_destination_returns_none_when_no_rules(db):
    assert relocate.resolve_destination(db, "GK1", "mybook", "EPUB") is None


def test_resolve_destination_format_only_rule_is_case_insensitive(db):
    db.add(DownloadDestination(name="Ebooks", formats="epub, mobi", tag_id=None, path="/mnt/books"))
    db.commit()
    assert relocate.resolve_destination(db, "GK1", "mybook", "EPUB") == Path("/mnt/books")


def test_resolve_destination_default_catch_all_when_formats_empty(db):
    db.add(DownloadDestination(name="Default", formats="", tag_id=None, path="/mnt/default"))
    db.commit()
    assert relocate.resolve_destination(db, "GK1", "anything", "ZIP") == Path("/mnt/default")


def test_resolve_destination_no_match_returns_none(db):
    db.add(DownloadDestination(name="Ebooks", formats="epub", tag_id=None, path="/mnt/books"))
    db.commit()
    assert relocate.resolve_destination(db, "GK1", "mybook", "PDF") is None


def test_resolve_destination_tag_and_format_beats_format_only(db):
    tag = Tag(name="Comic")
    db.add(tag)
    db.commit()
    db.add(BundleTag(tag_id=tag.id, gamekey="GK1"))
    db.add(DownloadDestination(name="Books", formats="pdf", tag_id=None, path="/mnt/books"))
    db.add(DownloadDestination(name="Comics", formats="pdf", tag_id=tag.id, path="/mnt/comics"))
    db.commit()

    assert relocate.resolve_destination(db, "GK1", "somebook", "PDF") == Path("/mnt/comics")
    # An untagged bundle with the same format still gets the format-only rule.
    assert relocate.resolve_destination(db, "GK2", "somebook", "PDF") == Path("/mnt/books")


def test_resolve_destination_tag_rule_does_not_match_wrong_format(db):
    tag = Tag(name="Comic")
    db.add(tag)
    db.commit()
    db.add(BundleTag(tag_id=tag.id, gamekey="GK1"))
    db.add(DownloadDestination(name="Comics", formats="cbz", tag_id=tag.id, path="/mnt/comics"))
    db.commit()

    assert relocate.resolve_destination(db, "GK1", "somebook", "PDF") is None


def test_resolve_destination_honors_item_level_tag_via_machine_name(db):
    """The comic-vs-book PDF case: a bundle isn't tagged at all, but this one
    subproduct is, via ItemTag keyed on machine_name.
    """
    tag = Tag(name="Comic")
    db.add(tag)
    db.commit()
    db.add(ItemTag(tag_id=tag.id, machine_name="graphicnovel1"))
    db.add(DownloadDestination(name="Comics", formats="pdf", tag_id=tag.id, path="/mnt/comics"))
    db.commit()

    assert relocate.resolve_destination(db, "GK1", "graphicnovel1", "PDF") == Path("/mnt/comics")
    assert relocate.resolve_destination(db, "GK1", "unrelated-item", "PDF") is None


# --- relocate() itself --------------------------------------------------------


def test_relocate_moves_file_and_returns_new_path(tmp_path):
    src_dir = tmp_path / "staging"
    src_dir.mkdir()
    src = src_dir / "book.epub"
    src.write_bytes(b"hello")
    dest_dir = tmp_path / "library" / "books"

    result = relocate.relocate(src, dest_dir)

    assert result == dest_dir / "book.epub"
    assert result.read_bytes() == b"hello"
    assert not src.exists()


def test_relocate_refuses_to_overwrite_an_existing_file_at_the_destination(tmp_path):
    """Two different items can share a filename (a generic "book.epub", or two
    different bundles) and resolve to the same destination — silently
    overwriting one would lose it outright.
    """
    src = tmp_path / "staging" / "book.epub"
    src.parent.mkdir()
    src.write_bytes(b"new content")
    dest_dir = tmp_path / "library"
    dest_dir.mkdir()
    (dest_dir / "book.epub").write_bytes(b"different, already-relocated file")

    with pytest.raises(FileExistsError):
        relocate.relocate(src, dest_dir)

    assert src.exists()  # left untouched
    assert (dest_dir / "book.epub").read_bytes() == b"different, already-relocated file"


# --- wired into worker._run_job() --------------------------------------------


def _seed_tagged_bundle(make_bundle, db, gamekey="GK1", machine_name="graphicnovel1", tag_name="Comic"):
    order = make_order(
        name="Test Bundle",
        subproducts=[
            make_subproduct(
                human_name="Graphic Novel",
                machine_name=machine_name,
                downloads=[{"download_struct": [{"name": "PDF", "file_size": 5, "url": {"web": "https://dl.humble.com/comic.pdf"}}]}],
            )
        ],
    )
    bundle = make_bundle(gamekey=gamekey, order=order)
    tag = Tag(name=tag_name)
    db.add(tag)
    db.commit()
    db.add(ItemTag(tag_id=tag.id, machine_name=machine_name))
    db.commit()
    return bundle, tag


@pytest.mark.asyncio
async def test_run_job_relocates_completed_download_when_destination_configured(db, make_bundle, tmp_path):
    bundle, tag = _seed_tagged_bundle(make_bundle, db)
    dest_dir = tmp_path / "comics"
    db.add(DownloadDestination(name="Comics", formats="pdf", tag_id=tag.id, path=str(dest_dir)))
    db.commit()

    staged = _predicted_path(bundle.name, "Graphic Novel", "comic.pdf")
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(b"x" * 5)

    job = DownloadJob(gamekey=bundle.gamekey, bundle_name=bundle.name)
    db.add(job)
    db.commit()
    db.refresh(job)

    with patch("app.downloads.worker.runner.run_download_job", new=AsyncMock(return_value=(0, "ok"))):
        await worker._run_job(job.id, bundle.gamekey, None, None)

    row = db.query(Download).filter(Download.gamekey == bundle.gamekey).one()
    assert row.current_location_path == str(dest_dir / "comic.pdf")
    assert (dest_dir / "comic.pdf").exists()
    assert not staged.exists()


@pytest.mark.asyncio
async def test_run_job_leaves_file_in_staging_when_no_destination_matches(db, make_bundle):
    bundle, _tag = _seed_tagged_bundle(make_bundle, db)
    # No DownloadDestination rows at all — today's unchanged behavior.
    staged = _predicted_path(bundle.name, "Graphic Novel", "comic.pdf")
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(b"x" * 5)

    job = DownloadJob(gamekey=bundle.gamekey, bundle_name=bundle.name)
    db.add(job)
    db.commit()
    db.refresh(job)

    with patch("app.downloads.worker.runner.run_download_job", new=AsyncMock(return_value=(0, "ok"))):
        await worker._run_job(job.id, bundle.gamekey, None, None)

    row = db.query(Download).filter(Download.gamekey == bundle.gamekey).one()
    assert row.current_location_path == str(staged)
    assert staged.exists()


@pytest.mark.asyncio
async def test_run_job_does_not_re_relocate_an_already_completed_row(db, make_bundle, tmp_path):
    """Re-verifying an already-completed row (e.g. a second trigger of the same
    download) must not try to move a file that's already been relocated away
    from its staging path — it would no longer be there.
    """
    bundle, tag = _seed_tagged_bundle(make_bundle, db)
    dest_dir = tmp_path / "comics"
    db.add(DownloadDestination(name="Comics", formats="pdf", tag_id=tag.id, path=str(dest_dir)))
    db.commit()

    staged = _predicted_path(bundle.name, "Graphic Novel", "comic.pdf")
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(b"x" * 5)

    job1 = DownloadJob(gamekey=bundle.gamekey, bundle_name=bundle.name)
    db.add(job1)
    db.commit()
    db.refresh(job1)
    with patch("app.downloads.worker.runner.run_download_job", new=AsyncMock(return_value=(0, "ok"))):
        await worker._run_job(job1.id, bundle.gamekey, None, None)

    already_at = (dest_dir / "comic.pdf")
    assert already_at.exists()

    # Second job: humble-cli would find the file already gone and presumably
    # skip re-fetching it, or the test simulates the row already being
    # complete regardless of what the CLI does this time.
    job2 = DownloadJob(gamekey=bundle.gamekey, bundle_name=bundle.name)
    db.add(job2)
    db.commit()
    db.refresh(job2)
    # Re-create the staging file to simulate humble-cli re-fetching it (it has
    # no way to know it was moved away) — the guard under test is what stops a
    # second, pointless relocation attempt here, not the CLI's own behavior.
    staged.write_bytes(b"x" * 5)

    with patch("app.downloads.worker.runner.run_download_job", new=AsyncMock(return_value=(0, "ok"))):
        await worker._run_job(job2.id, bundle.gamekey, None, None)

    row = db.query(Download).filter(Download.gamekey == bundle.gamekey).one()
    assert row.current_location_path == str(already_at)


@pytest.mark.asyncio
async def test_run_job_leaves_file_in_staging_when_destination_already_has_a_same_named_file(db, make_bundle, tmp_path):
    """Two different bundles' items can share a filename and the same
    destination rule (e.g. two generically-named "book.epub" files both
    routed to the same default Ebooks folder) — the second one to complete
    must not silently clobber the first.
    """
    bundle, tag = _seed_tagged_bundle(make_bundle, db, gamekey="GK1")
    dest_dir = tmp_path / "comics"
    db.add(DownloadDestination(name="Comics", formats="pdf", tag_id=tag.id, path=str(dest_dir)))
    db.commit()

    dest_dir.mkdir(parents=True)
    (dest_dir / "comic.pdf").write_bytes(b"a different item's file, already here")

    staged = _predicted_path(bundle.name, "Graphic Novel", "comic.pdf")
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(b"x" * 5)

    job = DownloadJob(gamekey=bundle.gamekey, bundle_name=bundle.name)
    db.add(job)
    db.commit()
    db.refresh(job)

    with patch("app.downloads.worker.runner.run_download_job", new=AsyncMock(return_value=(0, "ok"))):
        await worker._run_job(job.id, bundle.gamekey, None, None)

    row = db.query(Download).filter(Download.gamekey == bundle.gamekey).one()
    assert row.status == STATUS_COMPLETED  # the download itself still succeeded
    assert row.current_location_path == str(staged)  # left at staging, not relocated
    assert "failed to relocate" in row.error_message
    assert (dest_dir / "comic.pdf").read_bytes() == b"a different item's file, already here"
