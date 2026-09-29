"""MySQL adapter tests; run only when TAPASCRAPE_MYSQL_URL points at a scratch database."""

import os
from datetime import datetime

import pytest

from tapascrape.db import open_database
from tapascrape.db.schema import TABLES

URL = os.environ.get("TAPASCRAPE_MYSQL_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TAPASCRAPE_MYSQL_URL not set")


@pytest.fixture
def db():
    with open_database(URL) as db:
        for table in reversed(TABLES):
            db.execute(f"DROP TABLE IF EXISTS {db.quote_ident(table.name)}")
        db.create_schema()
        db.create_schema()  # idempotent
        yield db


def test_upsert_reserved_words_and_types(db):
    db.upsert_many("posts", [{"id": 5811, "topic_id": 6364, "user_id": 1, "index": 1,
                              "timestamp": datetime(2009, 1, 14, 4, 12, 7), "content": "<b>ñ 🙂</b>"}])
    db.upsert_many("posts", [{"id": 5811, "topic_id": 6364, "user_id": 1, "index": 2,
                              "timestamp": datetime(2009, 1, 14, 4, 12, 7), "content": "x"}])
    assert db.query('SELECT `index`, `timestamp` FROM posts') == [(2, datetime(2009, 1, 14, 4, 12, 7))]
    db.upsert_many("groups", [{"id": 5}, {"id": 5}])
    db.upsert_many("users", [{"id": 1, "name": "Keron Cyst"}])
    db.update_many("users", [{"id": 1, "rank": "Fossil"}])
    assert db.query("SELECT name, `rank` FROM users") == [("Keron Cyst", "Fossil")]
    assert db.insert("avatars", {"user_id": 1, "data": b"\x00\x01"}) == 1
    db.set_state("topic-posts:1", "done")
    assert db.states("topic-posts:") == {"1": "done"}
    db.commit()
