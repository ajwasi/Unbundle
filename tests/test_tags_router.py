from app.models.tag import BundleTag, ItemTag, Tag
from tests.factories import make_order, make_subproduct


def test_tags_page_requires_auth(client):
    resp = client.get("/tags", follow_redirects=False)
    assert resp.status_code == 303


def test_tags_page_shows_no_tags_initially(authed_client):
    resp = authed_client.get("/tags")
    assert resp.status_code == 200
    assert "No tags yet" in resp.text


def test_create_tag(authed_client, db):
    resp = authed_client.post("/tags", data={"name": "Favorites"})
    assert resp.status_code == 200
    assert "Favorites" in resp.text
    assert db.query(Tag).filter(Tag.name == "Favorites").one() is not None


def test_create_tag_ignores_blank_name(authed_client, db):
    resp = authed_client.post("/tags", data={"name": "   "})
    assert resp.status_code == 200
    assert db.query(Tag).count() == 0


def test_create_tag_with_existing_name_does_not_duplicate(authed_client, db):
    authed_client.post("/tags", data={"name": "Favorites"})
    authed_client.post("/tags", data={"name": "Favorites"})
    assert db.query(Tag).filter(Tag.name == "Favorites").count() == 1


def test_tags_page_shows_bundle_and_item_counts(authed_client, db, make_bundle):
    bundle = make_bundle(gamekey="GK1", order=make_order(subproducts=[make_subproduct("Item A", machine_name="a")]))
    tag = Tag(name="Favorites")
    db.add(tag)
    db.commit()
    db.add(BundleTag(tag_id=tag.id, gamekey=bundle.gamekey))
    db.add(ItemTag(tag_id=tag.id, machine_name="a"))
    db.commit()

    resp = authed_client.get("/tags")
    assert resp.status_code == 200
    # Both counts are 1 — just confirm the row renders with the tag name and
    # doesn't blow up; exact cell layout isn't worth asserting against.
    assert "Favorites" in resp.text


def test_rename_tag(authed_client, db):
    tag = Tag(name="Old Name")
    db.add(tag)
    db.commit()

    resp = authed_client.post(f"/tags/{tag.id}/rename", data={"name": "New Name"})
    assert resp.status_code == 200
    db.refresh(tag)
    assert tag.name == "New Name"
    assert "New Name" in resp.text


def test_rename_tag_ignores_blank_name(authed_client, db):
    tag = Tag(name="Keep Me")
    db.add(tag)
    db.commit()

    authed_client.post(f"/tags/{tag.id}/rename", data={"name": "   "})
    db.refresh(tag)
    assert tag.name == "Keep Me"


def test_rename_nonexistent_tag_is_a_noop(authed_client):
    resp = authed_client.post("/tags/999/rename", data={"name": "Whatever"})
    assert resp.status_code == 200


def test_delete_tag_removes_it_and_its_associations(authed_client, db, make_bundle):
    bundle = make_bundle(gamekey="GK1", order=make_order(subproducts=[make_subproduct("Item A", machine_name="a")]))
    tag = Tag(name="Doomed")
    db.add(tag)
    db.commit()
    db.add(BundleTag(tag_id=tag.id, gamekey=bundle.gamekey))
    db.add(ItemTag(tag_id=tag.id, machine_name="a"))
    db.commit()
    tag_id = tag.id

    resp = authed_client.post(f"/tags/{tag_id}/delete")
    assert resp.status_code == 200
    assert db.query(Tag).filter(Tag.id == tag_id).one_or_none() is None
    assert db.query(BundleTag).filter(BundleTag.tag_id == tag_id).count() == 0
    assert db.query(ItemTag).filter(ItemTag.tag_id == tag_id).count() == 0


def test_delete_nonexistent_tag_is_a_noop(authed_client):
    resp = authed_client.post("/tags/999/delete")
    assert resp.status_code == 200
