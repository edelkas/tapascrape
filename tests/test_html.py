"""HTML parsers and the enrich pass, against pages saved from metanetfr."""

from datetime import datetime
from pathlib import Path

import pytest

from tapascrape.config import BoardConfig
from tapascrape.crawl.enrich import SOURCE_PREFIX, enrich_profiles, pending_profiles, recover_gaps
from tapascrape.crawl.posts import GAP_PREFIX
from tapascrape.db import open_database
from tapascrape.models import Group
from tapascrape.net.web import is_challenge
from tapascrape.parse.profile import parse_profile
from tapascrape.parse.topic import parse_topic_posts

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_profile_keron():
    profile = parse_profile(fixture("profile_keron.html"))
    assert profile.rank == "Fossil"
    assert profile.signature.startswith("loLol<br/><img")
    assert profile.groups == [Group(5, "Administrators"), Group(4, "Global moderators"),
                              Group(2, "Registered users")]


def test_profile_missing_user():
    assert parse_profile(fixture("profile_missing.html")) is None


def test_topic_posts():
    posts = {p.id: p for p in parse_topic_posts(fixture("topic_199_s1727.html"))}
    assert len(posts) == 10
    post = posts[112460]  # the one the API can't return
    assert (post.index, post.user_id, post.author_name) == (1728, 9370552, "LittleViking")
    assert post.timestamp == datetime(2005, 11, 5, 0, 17)
    assert "Igel" in post.content and post.content.endswith("Anyway, post edited.")
    assert "post_body_end" not in post.content


def test_challenge_detection():
    assert is_challenge("<html><head><title>Just a moment...</title></head></html>")
    assert not is_challenge(fixture("profile_keron.html"))


class FakeWeb:
    def __init__(self, pages):
        self.config = BoardConfig("metanetfr")
        self.pages = pages
        self.requested = []

    def get(self, path):
        self.requested.append(path)
        return self.pages[path]


@pytest.fixture
def db():
    with open_database(":memory:") as db:
        db.create_schema()
        yield db


def test_enrich_profiles(db):
    db.upsert_many("users", [{"id": 9370387, "name": "Keron Cyst", "post_count": 7652}])
    db.upsert_many("users", [{"id": 123, "name": "Gone"}])
    web = FakeWeb({
        "https://www.tapatalk.com/groups/metanetfr/memberlist.php?mode=viewprofile&u=9370387":
            fixture("profile_keron.html"),
        "https://www.tapatalk.com/groups/metanetfr/memberlist.php?mode=viewprofile&u=123":
            fixture("profile_missing.html"),
    })
    assert enrich_profiles(web, db) == 1
    assert db.query('SELECT name, "rank", post_count, substr(signature, 1, 5) FROM users '
                    "WHERE id = 9370387") == [("Keron Cyst", "Fossil", 7652, "loLol")]
    assert db.query("SELECT id, name FROM groups ORDER BY id") == [
        (2, "Registered users"), (4, "Global moderators"), (5, "Administrators")]
    assert db.query("SELECT COUNT(*) FROM group_users WHERE user_id = 9370387") == [(3,)]
    assert db.get_state("profile:123") == "missing"
    assert pending_profiles(db) == []


def test_recover_gaps(db):
    db.set_state(f"{GAP_PREFIX}199:1727", "SQL ERROR ...")
    web = FakeWeb({"viewtopic.php?t=199&start=1727": fixture("topic_199_s1727.html")})
    assert recover_gaps(web, db) == 1
    assert db.query('SELECT id, topic_id, user_id, "index", "timestamp" FROM posts') == [
        (112460, 199, 9370552, 1728, "2005-11-05 00:17:00")]
    assert db.query("SELECT name FROM users WHERE id = 9370552") == [("LittleViking",)]
    assert db.states(GAP_PREFIX) == {}
    assert db.get_state(f"{SOURCE_PREFIX}112460") == "html"
