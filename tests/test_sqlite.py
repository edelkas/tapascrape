import io
from datetime import datetime

import pytest

from tapascrape.crawl.forums import ForumCounts, print_tree, store_forums
from tapascrape.db import open_database
from tapascrape.models import Forum


@pytest.fixture
def db():
    with open_database("sqlite:///:memory:") as db:
        db.create_schema()
        yield db


def tree():
    root = Forum(64, None, "N", is_category=True)
    news = Forum(1, 64, "News")
    news.children = [Forum(54, 1, "The Legacy Discussion Archive", "I'm too tasty for this subforum.")]
    root.children = [news]
    return [root]


def test_create_schema_is_idempotent(db):
    db.create_schema()
    names = {r[0] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"forums", "topics", "posts", "users", "avatars", "groups", "group_users"} <= names


def test_store_forums(db):
    assert store_forums(db, tree()) == 3
    assert db.query('SELECT id, parent_id, name FROM forums ORDER BY id') == [
        (1, 64, "News"), (54, 1, "The Legacy Discussion Archive"), (64, None, "N")]


def test_upsert_then_partial_update_keeps_other_columns(db):
    db.upsert_many("users", [{"id": 1, "name": "Keron", "post_count": 1}])
    db.upsert_many("users", [{"id": 1, "name": "Keron Cyst", "post_count": 7652,
                              "joined_at": datetime(2004, 4, 2, 22, 35, 42)}])
    db.update_many("users", [{"id": 1, "rank": "Fossil"}])
    assert db.query('SELECT name, "rank", post_count, joined_at FROM users') == [
        ("Keron Cyst", "Fossil", 7652, "2004-04-02 22:35:42")]


def test_reserved_word_columns(db):
    db.upsert_many("posts", [{"id": 5811, "topic_id": 6364, "user_id": 1, "index": 1,
                              "timestamp": datetime(2009, 1, 14, 4, 12, 7), "content": "<b>hi</b>"}])
    assert db.query('SELECT "index" FROM posts WHERE id = ?', (5811,)) == [(1,)]
    db.upsert_many("group_users", [{"group_id": 5, "user_id": 1}, {"group_id": 5, "user_id": 1}])
    assert db.query("SELECT COUNT(*) FROM group_users") == [(1,)]


def test_avatar_autoincrement_and_state(db):
    assert db.insert("avatars", {"user_id": 1, "data": b"\x89PNG"}) == 1
    assert db.get_state("topic:1") is None
    db.set_state("topic:1", "done")
    assert db.get_state("topic:1") == "done"


def test_transaction_rolls_back(db):
    with pytest.raises(RuntimeError):
        with db.transaction():
            db.upsert_many("groups", [{"id": 5, "name": "Administrators"}])
            raise RuntimeError
    assert db.query("SELECT COUNT(*) FROM groups") == [(0,)]


def test_print_tree():
    counts = {64: ForumCounts(0, 0), 1: ForumCounts(78, 1000), 54: ForumCounts(99, 5000)}
    out = io.StringIO()
    total = print_tree(tree(), counts, out)
    assert out.getvalue().splitlines() == [
        "[64] N (category) — topics: 177, posts: 6,000",
        "    [1] News — topics: 78, posts: 1,000  (with subforums: topics: 177, posts: 6,000)",
        "        [54] The Legacy Discussion Archive — topics: 99, posts: 5,000",
    ]
    assert (total.topics, total.posts) == (177, 6000)
