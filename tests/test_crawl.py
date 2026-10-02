"""End-to-end crawl against a fake API and an in-memory SQLite database."""

from datetime import datetime

import pytest

from tapascrape.config import BoardConfig
from tapascrape.crawl import users as users_module
from tapascrape.crawl.finalize import finalize
from tapascrape.crawl.forums import store_forums
from tapascrape.crawl.posts import GAP_PREFIX, crawl_posts, pending_topics
from tapascrape.crawl.sources import GAP_PREFIX as SOURCE_GAP
from tapascrape.crawl.sources import SourcesAborted, crawl_sources, pending_sources
from tapascrape.crawl.topics import crawl_topics
from tapascrape.crawl.users import crawl_members, crawl_users, pending_users
from tapascrape.db import open_database
from tapascrape.models import Forum
from tapascrape.net.api import ApiError, AuthError, SplitError, TapatalkApi

T0 = 1231906327  # 2009-01-14 04:12:07 UTC

# topic id -> (forum, sticky, closed, views, [(post id, author id, unix time)])
TOPICS = {
    6364: ("54", True, False, 4644,
           [(5811 + i, (9370681, 9371303)[i % 2], T0 + 60 * i) for i in range(60)]),
    24242: ("54", False, True, 342, [(900, 9370441, T0 - 1000), (901, 9371303, T0 - 500)]),
    7000: ("1", False, False, 5, [(1000, 42, T0 + 99999)]),
}
USERS = {
    9370681: ("Kablizzy", ["2"], ""),
    9371303: ("Captain Planet", ["5", "2"], "https://cdn.test/cp.png"),
    9370441: ("maximo", ["2"], ""),
    # 42 is a deleted account: get_user_info fails.
}


def source_of(pid):
    if pid == 901:  # quotes two people: contains the seam used to split batches
        return '[quote="a"]x[/quote]\n[quote="b"]y[/quote]\nme too'
    return f"[b]{pid}[/b]\n[color=red]hi[/color]"


class FakeApi(TapatalkApi):
    def __init__(self, fail_topic_after=None, broken_offsets=()):
        super().__init__(BoardConfig("test", rate=0))
        self.fail_topic_after = fail_topic_after  # (topic id, start) to raise at, once
        self.broken_offsets = set(broken_offsets)  # (topic id, offset) the server can't render
        self.calls = []

    def call(self, method, *params):
        self.calls.append((method, params))
        return getattr(self, "_" + method)(*params)

    def _get_topic(self, fid, start, end, mode=""):
        rows = [
            {"topic_id": str(tid), "forum_id": f, "topic_title": f"T{tid}",
             "topic_author_id": str(posts[0][1]), "topic_author_name": USERS.get(posts[0][1], ("Ghost",))[0],
             "is_sticky": sticky, "is_closed": closed, "view_number": views,
             "total_post_num": len(posts)}
            for tid, (f, sticky, closed, views, posts) in TOPICS.items()
            if f == fid and (mode == "TOP") == sticky and mode != "ANN"]
        return {"total_topic_num": len(rows), "topics": rows[start:end + 1]}

    def _get_thread(self, tid, start, end, html):
        if self.fail_topic_after == (int(tid), start):
            self.fail_topic_after = None
            raise ApiError("boom")
        if any((int(tid), o) in self.broken_offsets for o in range(start, end + 1)):
            raise ApiError("get_thread: (SQL-ERROR-CODE:1267) SQL ERROR [ mysqli ]\n\nIllegal mix")
        posts = TOPICS[int(tid)][4]
        return {"total_post_num": len(posts), "posts": [
            {"post_id": str(pid), "post_author_id": str(uid),
             "post_author_name": USERS.get(uid, ("Ghost",))[0], "position": i + 1,
             "timestamp": str(ts), "post_content": f"<p>{pid}</p>"}
            for i, (pid, uid, ts) in enumerate(posts) if start <= i <= end]}

    unquotable = {5850}  # post ids that make the server's quote feature fail
    refused = {5860}  # readable posts the quote feature rejects anyway
    logged_out = False

    def _get_quote_post(self, ids):
        pids = [int(i) for i in ids.split("-")]
        if self.logged_out:
            raise AuthError("get_quote_post: You are not logged in or you do not have permission")
        if self.unquotable & set(pids):
            raise ApiError("get_quote_post: (SQL-ERROR-CODE:1267) SQL ERROR [ mysqli ]")
        if self.refused & set(pids):
            raise ApiError("get_quote_post: Need valid post id!")
        authors = {pid: uid for *_, posts in TOPICS.values() for pid, uid, _ in posts}
        return {"post_id": ids, "post_title": "", "post_content": "".join(
            f'[quote="{USERS.get(authors[pid], ("Ghost",))[0]}"]{source_of(pid)}[/quote]\n'
            for pid in pids)}

    def _get_member_list(self, page, per_page):
        members = [self._get_user_info("", str(uid)) for uid in sorted(USERS)]
        members.append({"user_id": "555", "username": "Lurker", "usergroup_id": ["2"],
                        "post_count": 0, "timestamp_reg": "1080945342", "timestamp": "",
                        "icon_url": ""})
        start = (max(page, 1) - 1) * per_page
        return {"result": True, "member_count": len(members),
                "list": {m["user_id"]: m for m in members[start:start + per_page]}}

    def _get_user_info(self, _name, uid):
        if int(uid) not in USERS:
            raise ApiError("user not found")
        name, groups, icon = USERS[int(uid)]
        return {"user_id": uid, "username": name, "usergroup_id": groups, "post_count": 10,
                "timestamp_reg": "1080945342", "timestamp": "1574790215", "icon_url": icon}


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(users_module, "fetch_bytes", lambda url, throttle: b"PNG:" + url.encode())
    with open_database(":memory:") as db:
        db.create_schema()
        yield db


def forums():
    news = Forum(1, 64, "News", children=[Forum(54, 1, "The Legacy Discussion Archive")])
    return [Forum(64, None, "N", is_category=True, children=[news])]


def all_forums():
    return [f for root in forums() for f, _ in root.walk()]


def run_crawl(api, db):
    store_forums(db, forums())
    crawl_topics(api, db, all_forums())
    crawl_posts(api, db)
    crawl_users(api, db)
    finalize(db)


def test_full_crawl(db):
    api = FakeApi()
    run_crawl(api, db)

    assert db.query("SELECT id, forum_id, stickied, locked, view_count, post_count, last_post_id, "
                    "created_at FROM topics ORDER BY id") == [
        (6364, 54, 1, 0, 4644, 60, 5870, "2009-01-14 04:12:07"),
        (7000, 1, 0, 0, 5, 1, 1000, "2009-01-15 07:58:46"),
        (24242, 54, 0, 1, 342, 2, 901, "2009-01-14 03:55:27"),
    ]
    assert db.query('SELECT id, "index", user_id, "timestamp" FROM posts WHERE id IN (5811, 5870) '
                    'ORDER BY id') == [
        (5811, 1, 9370681, "2009-01-14 04:12:07"), (5870, 60, 9371303, "2009-01-14 05:11:07")]
    assert db.query("SELECT id, post_count, view_count, last_post_id FROM forums ORDER BY id") == [
        (1, 1, 5, 1000), (54, 62, 4986, 5870), (64, 0, 0, None)]

    users = {r[0]: r[1:] for r in db.query(
        "SELECT id, name, joined_at, post_count, avatar_id FROM users")}
    assert users[9371303][:3] == ("Captain Planet", "2004-04-02 22:35:42", 10)
    assert users[42] == ("Ghost", None, None, None)  # seeded from posts, profile gone
    assert users[9370681][3] is None  # no avatar
    avatar = db.query("SELECT user_id, data FROM avatars WHERE id = ?", (users[9371303][3],))
    assert avatar == [(9371303, b"PNG:https://cdn.test/cp.png")]
    assert sorted(db.query("SELECT group_id, user_id FROM group_users WHERE user_id = 9371303")) == [
        (2, 9371303), (5, 9371303)]
    assert db.query("SELECT id, name FROM groups ORDER BY id") == [(2, None), (5, None)]
    assert pending_topics(db, None) == [] and pending_users(db) == []


def test_crawl_is_idempotent(db):
    run_crawl(FakeApi(), db)
    second = FakeApi()
    run_crawl(second, db)
    # Nothing left to fetch: only the forum listings' topic calls are skipped too.
    assert not [c for c in second.calls if c[0] in ("get_thread", "get_user_info", "get_topic")]
    assert db.query("SELECT COUNT(*) FROM posts") == [(63,)]
    assert db.query("SELECT COUNT(*) FROM avatars") == [(1,)]


def test_posts_resume_after_failure(db):
    store_forums(db, forums())
    api = FakeApi(fail_topic_after=(6364, 50))
    crawl_topics(api, db, all_forums())
    crawl_posts(api, db)
    # First page of 6364 was stored; the topic is still pending from offset 50.
    assert (6364, 50, 60) in pending_topics(db, None)
    assert db.query("SELECT COUNT(*) FROM posts WHERE topic_id = 6364") == [(50,)]

    retry = FakeApi()
    crawl_posts(retry, db)
    assert [c[1][:2] for c in retry.calls if c[0] == "get_thread"] == [("6364", 50)]
    assert db.query("SELECT COUNT(*) FROM posts WHERE topic_id = 6364") == [(60,)]
    assert pending_topics(db, None) == []


def test_unrenderable_posts_are_isolated_and_skipped(db):
    store_forums(db, forums())
    api = FakeApi(broken_offsets={(6364, 27), (6364, 28), (24242, 0)})
    crawl_topics(api, db, all_forums())
    crawl_posts(api, db)
    indexes = {r[0] for r in db.query('SELECT "index" FROM posts WHERE topic_id = 6364')}
    assert indexes == set(range(1, 61)) - {28, 29}
    assert db.query("SELECT id FROM posts WHERE topic_id = 24242") == [(901,)]
    assert set(db.states(GAP_PREFIX)) == {"6364:27", "6364:28", "24242:0"}
    assert pending_topics(db, None) == []  # gaps don't keep topics pending
    # Bisection stays cheap: far fewer calls than one per post.
    assert len([c for c in api.calls if c[0] == "get_thread"]) < 25


def test_other_api_errors_are_not_bisected(db):
    store_forums(db, forums())
    api = FakeApi(fail_topic_after=(6364, 0))
    crawl_topics(api, db, all_forums())
    crawl_posts(api, db)
    assert [c[1][1] for c in api.calls if c[0] == "get_thread" and c[1][0] == "6364"] == [0]
    assert db.states(GAP_PREFIX) == {}


def test_members_include_non_posters_and_leave_deleted_authors_to_crawl_users(db):
    store_forums(db, forums())
    api = FakeApi()
    crawl_topics(api, db, all_forums())
    crawl_posts(api, db)
    assert crawl_members(api, db, per_page=2) == 4
    assert db.query("SELECT name, post_count, last_active_at FROM users WHERE id = 555") == [
        ("Lurker", 0, None)]
    assert db.query("SELECT COUNT(*) FROM avatars") == [(1,)]
    # Only the deleted author (42) is left for per-user fetching.
    assert pending_users(db) == [42]
    calls = len(api.calls)
    crawl_users(api, db)
    assert [c[0] for c in api.calls[calls:]] == ["get_user_info"]
    # A second walk of the list stores nothing new.
    assert crawl_members(api, db, per_page=2) == 0


def test_quote_posts_splits_batches():
    api = FakeApi()
    assert api.quote_posts([5811, 5812]) == {5811: source_of(5811), 5812: source_of(5812)}
    assert api.quote_posts([901]) == {901: source_of(901)}
    with pytest.raises(SplitError):
        api.quote_posts([900, 901, 1000])


def test_sources_crawl_isolates_problem_posts(db):
    store_forums(db, forums())
    api = FakeApi()
    crawl_topics(api, db, all_forums())
    crawl_posts(api, db)
    assert len(pending_sources(db)) == 63
    assert crawl_sources(api, db, batch_size=16) == 61
    assert db.query("SELECT source FROM posts WHERE id = 901") == [(source_of(901),)]
    assert db.query("SELECT source FROM posts WHERE id = 5811") == [(source_of(5811),)]
    assert db.query("SELECT source FROM posts WHERE id = 5850") == [(None,)]
    assert set(db.states(SOURCE_GAP)) == {"5850", "5860"}
    assert pending_sources(db) == []  # the gap isn't retried
    calls = len(api.calls)
    assert crawl_sources(api, db) == 0 and len(api.calls) == calls
    # Re-crawling posts keeps the sources.
    db.execute("DELETE FROM crawl_state WHERE key LIKE 'topic-posts:%'")
    crawl_posts(api, db)
    assert db.query("SELECT source FROM posts WHERE id = 5811") == [(source_of(5811),)]


def test_sources_expired_session_is_not_a_bad_post(db):
    store_forums(db, forums())
    api = FakeApi()
    crawl_topics(api, db, all_forums())
    crawl_posts(api, db)
    api.logged_out = True
    with pytest.raises(AuthError):
        crawl_sources(api, db)
    assert db.states(SOURCE_GAP) == {}
    assert len(pending_sources(db)) == 63


def test_sources_stop_when_a_whole_batch_fails(db, monkeypatch):
    store_forums(db, forums())
    api = FakeApi()
    crawl_topics(api, db, all_forums())
    crawl_posts(api, db)
    monkeypatch.setattr(FakeApi, "refused", set(range(10000)))  # the board refuses everything
    with pytest.raises(SourcesAborted):
        crawl_sources(api, db, batch_size=20)
    assert db.states(SOURCE_GAP) == {}  # nothing marked: it's not the posts' fault


def test_sources_retry_gaps(db):
    store_forums(db, forums())
    api = FakeApi()
    crawl_topics(api, db, all_forums())
    crawl_posts(api, db)
    crawl_sources(api, db)
    assert set(db.states(SOURCE_GAP)) == {"5850", "5860"}
    api.refused = set()
    assert crawl_sources(api, db, retry_gaps=True) == 1  # 5860 now works; 5850 still fails
    assert set(db.states(SOURCE_GAP)) == {"5850"}


def test_cli_reports_errors_without_traceback(monkeypatch, caplog):
    from tapascrape import cli

    def boom(args):
        raise ApiError("get_quote_post: Need valid post id!")

    monkeypatch.setattr(cli, "cmd_status", boom)
    assert cli.main(["status", "--db", ":memory:"]) == 1
    assert "ApiError: get_quote_post: Need valid post id!" in caplog.text
    assert "rerun the same command to resume" in caplog.text


def test_forum_filter(db):
    store_forums(db, forums())
    api = FakeApi()
    crawl_topics(api, db, [f for f in all_forums() if f.id == 54])
    crawl_posts(api, db, [54])
    assert {r[0] for r in db.query("SELECT DISTINCT topic_id FROM posts")} == {6364, 24242}


def test_finalize_keeps_listing_counts_for_unscraped_topics(db):
    store_forums(db, forums())
    crawl_topics(FakeApi(), db, all_forums())
    finalize(db)
    assert db.query("SELECT post_count, created_at FROM topics WHERE id = 6364") == [(60, None)]
    assert db.query("SELECT post_count FROM forums WHERE id = 54") == [(62,)]


def test_schema_migration_adds_new_columns(tmp_path):
    path = tmp_path / "old.db"
    import sqlite3
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE topics (id INTEGER NOT NULL, forum_id INTEGER NOT NULL, user_id INTEGER, '
                 'name TEXT NOT NULL, stickied INTEGER NOT NULL, post_count INTEGER, '
                 'view_count INTEGER, last_post_id INTEGER, PRIMARY KEY (id))')
    conn.execute("INSERT INTO topics VALUES (1, 1, 1, 'x', 0, 1, 1, NULL)")
    conn.commit()
    conn.close()
    with open_database(str(path)) as db:
        db.create_schema()
        assert db.query("SELECT id, locked, created_at FROM topics") == [(1, None, None)]
        db.upsert_many("topics", [{"id": 2, "forum_id": 1, "name": "y", "stickied": False,
                                   "locked": True, "created_at": datetime(2020, 1, 1)}])
        assert db.query("SELECT locked FROM topics WHERE id = 2") == [(1,)]
