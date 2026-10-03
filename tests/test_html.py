"""HTML parsers and the enrich pass, against pages saved from metanetfr."""

from datetime import datetime
from pathlib import Path

import pytest

from tapascrape.config import BoardConfig
from tapascrape.crawl.enrich import SOURCE_PREFIX, enrich_profiles, pending_profiles, recover_gaps
from tapascrape.crawl.posts import GAP_PREFIX
from tapascrape.crawl.sources import GAP_PREFIX as SOURCE_GAP
from tapascrape.crawl.sources import ORIGIN_PREFIX, recover_sources
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


def test_recover_sources(db):
    # 112460 and 112462 have no source; 112481 has one, which must be left alone.
    db.upsert_many("posts", [
        {"id": 112460, "topic_id": 199, "index": 1728, "content": "api", "source": None},
        {"id": 112462, "topic_id": 199, "index": 1729, "content": "api", "source": None},
        {"id": 112481, "topic_id": 199, "index": 1730, "content": "api", "source": "original"},
    ])
    for post_id in (112460, 112462, 112481):
        db.set_state(f"{SOURCE_GAP}{post_id}", "SQL ERROR ...")
    web = FakeWeb({"viewtopic.php?t=199&start=1727": fixture("topic_199_s1727.html")})
    assert recover_sources(web, db) == 2
    assert web.requested == ["viewtopic.php?t=199&start=1727"]  # one page for both posts
    sources = dict(db.query("SELECT id, source FROM posts"))
    assert sources[112462] == ("[table][tr][td][b]QUOTE[/b] (spzbt @ Nov 5 2005, 12:12 AM)[/td][/tr]"
                               "[tr][td] Wow, that's weird.  Look up at the post kyubbi... It's "
                               "messed up.  [/td][/tr][/table] \n nothing looks wierd on my "
                               "computer..nothing at all")
    assert sources[112460].endswith("Very strange...\n\nAnyway, post edited.")
    assert sources[112481] == "original"
    assert db.query("SELECT content FROM posts WHERE id = 112460") == [("api",)]
    assert set(db.states(ORIGIN_PREFIX)) == {"112460", "112462"}
    assert set(db.states(SOURCE_GAP)) == {"112481"}
    assert recover_sources(web, db) == 0


class FakeResponse:
    def __init__(self, content, headers):
        self.status_code, self.content, self.headers = 200, content, headers
        self.text = ""


def test_get_bytes_asks_for_the_unpolished_original(monkeypatch):
    from tapascrape.net.throttle import RetryableError, Throttle
    from tapascrape.net.web import WebClient
    web = WebClient(BoardConfig("metanetfr", max_retries=0), throttle=Throttle(0), use_browser=False)
    requested = []
    answers = [FakeResponse(b"GIF89a original", {"content-type": "image/gif"}),
               FakeResponse(b"GIF89a polished", {"content-type": "image/gif", "cf-polished": "ok"})]
    monkeypatch.setattr(web.session, "get", lambda url, headers: requested.append(url) or answers.pop(0))
    assert web.get_bytes("https://x/smilies/4.gif", original=True) == b"GIF89a original"
    assert requested[0].startswith("https://x/smilies/4.gif?nocache=")
    with pytest.raises(RetryableError):
        web.get_bytes("https://x/smilies/4.gif", original=True)
    assert requested[1] != requested[0]  # a new cache-busting value each time
