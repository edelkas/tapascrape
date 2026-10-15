from tapascrape.db import open_database
from tapascrape.stats import board_stats


def rows(tables, title):
    table = next(t for t in tables if t.title == title)
    return {row[0]: row[1:] for row in table.rows}


def test_board_stats():
    with open_database(":memory:") as db:
        db.create_schema()
        db.upsert_many("forums", [{"id": 1, "parent_id": None, "name": "Category", "description": None},
                                  {"id": 2, "parent_id": 1, "name": "Old", "description": "talk"}])
        db.upsert_many("topics", [
            {"id": 5, "forum_id": 2, "user_id": 1, "name": "First", "stickied": True, "locked": False,
             "created_at": "2006-01-01 10:00:00", "post_count": 3, "has_poll": True},
            {"id": 6, "forum_id": 2, "user_id": 2, "name": "Polled", "stickied": False, "locked": True,
             "created_at": "2007-01-01 10:00:00", "post_count": 1, "has_poll": True}])
        db.upsert_many("posts", [
            {"id": 10, "topic_id": 5, "user_id": 1, "index": 1, "timestamp": "2006-01-01 10:00:00",
             "source": "a", "source_fixed": "a"},
            {"id": 11, "topic_id": 5, "user_id": 0, "index": 2, "timestamp": "2006-01-02 10:00:00",
             "source": "b", "source_fixed": "fixed"},
            {"id": 12, "topic_id": 6, "user_id": 1, "index": 1, "timestamp": "2007-01-01 10:00:00",
             "source": None, "source_fixed": None}])
        db.upsert_many("users", [{"id": 1, "name": "alice", "joined_at": "2005-06-01 00:00:00", "avatar_id": 1,
                                  "signature": "<i>sig</i>"},
                                 {"id": 2, "name": "2", "joined_at": None, "avatar_id": None, "signature": None}])
        db.upsert_many("groups", [{"id": 5, "name": "Admins"}])
        db.upsert_many("group_users", [{"group_id": 5, "user_id": 1}])
        db.upsert_many("polls", [{"topic_id": 6, "title": "?", "vote_count": 7, "max_options": 2}])
        db.upsert_many("attachments", [
            {"id": 1, "kind": "upload", "url": "u1", "uses": 2, "first_post_id": 10, "data": b"x"},
            {"id": 2, "kind": "attach-id", "url": "u2", "uses": 1, "first_post_id": None, "data": None},
            {"id": 3, "kind": "upload", "url": "u3", "uses": 0, "first_post_id": None, "data": None}])  # listed, unused
        db.set_state("attachment-missing:2", "not archived")
        db.upsert_many("quotes", [{"post_id": 11, "position": 0, "level": 1, "quoted_post_id": 10,
                                   "match": "author+date"},
                                  {"post_id": 11, "position": 1, "level": 2, "quoted_post_id": None, "match": "ambiguous"}])
        tables = board_stats(db)

    overview = rows(tables, "Overview")
    assert overview["Posts"] == [3] and overview["Users"] == [2] and overview["Attachments"] == [2]
    assert rows(tables, "Topics and posts by forum") == {"Category": [1, 0, 0], "  Old": [2, 2, 3]}
    topics = rows(tables, "Topics")
    assert topics["  with as many posts as Tapatalk counts"] == [1] and topics["  with fewer"] == [1]
    assert topics["Pinned"] == [1] and topics["  2-10 posts"] == [1] and topics["  1 post"] == [1]
    posts = rows(tables, "Posts")
    assert posts["  by guests"] == [1] and posts["  repaired by `fix`"] == [1]
    assert posts["Without it: not fetched yet"] == [1]
    users = rows(tables, "Users")
    assert users["  with posts"] == [1] and users["    2-10 posts"] == [1] and users["  without posts"] == [1]
    assert users["Named only by their id"] == [1] and users["With an avatar"] == [1]
    assert rows(tables, "Groups") == {"Admins": [5, 1]}
    assert rows(tables, "By year") == {"2005": [0, 0, 0, 1], "2006": [1, 2, 1, 0], "2007": [1, 1, 1, 0],
                                       "unknown": [0, 0, 0, 1]}
    polls = rows(tables, "Polls")
    assert polls["With the poll's results"] == [1, 7]
    assert polls["Listed with a poll, results not fetched yet"] == [1, None]
    attachments = rows(tables, "Attachments")
    assert attachments["All"] == [2, 3, 1, 1, 0]
    assert attachments["Used only in signatures"] == [1, 1, 0, 1, 0]
    quotes = rows(tables, "Quotes")
    assert quotes["  by author+date"] == [1] and quotes["Not linked: several posts fit as well"] == [1]
