from tapascrape.boards.metanet.schema import TABLES
from tapascrape.boards.metanet.site import build_metanet_site
from tapascrape.db import open_database

GIF = b"GIF89a\x01\x00\x01\x00\x00\x00\x00;"


def uniform(rows):
    """upsert_many writes the first row's columns: give every row all of them."""
    keys = {key for row in rows for key in row}
    return [{key: row.get(key) for key in keys} for row in rows]
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8


def test_metanet_site(tmp_path):
    with open_database(":memory:") as db:
        db.create_schema()
        db.create_schema(TABLES)
        db.upsert_many("forums", [{"id": 7, "parent_id": 64, "name": "General Community", "last_post_id": 11},
                                  {"id": 64, "parent_id": None, "name": "N", "last_post_id": None}])
        db.upsert_many("topics", [{"id": 5, "forum_id": 7, "user_id": 0, "name": "Kept", "stickied": False,
                                   "locked": False, "created_at": "2006-01-01 10:00:00", "post_count": 2,
                                   "view_count": 3, "last_post_id": 11}])
        db.upsert_many("posts", [
            {"id": 10, "topic_id": 5, "user_id": 0, "index": 1, "timestamp": "2006-01-01 10:00:00",
             "content": "", "source": "x", "source_fixed": "first"},
            {"id": 11, "topic_id": 5, "user_id": 9370595, "index": 2, "timestamp": "2006-01-02 10:00:00",
             "content": "", "source": "x", "source_fixed": "see [ts:topic=9 old_post=501]that[/ts:topic]"}])
        db.upsert_many("users", [{"id": 9370595, "name": "9370595", "avatar_id": 1}])
        db.upsert_many("avatars", [{"id": 1, "user_id": 9370595, "data": GIF}])
        db.upsert_many("forumer_forums", [
            {"id": 40, "name": "N Webcomics", "description": "Comics", "parent_id": 7, "category": "N",
             "in_tapatalk": False},
            {"id": 19, "name": "N Images", "description": None, "parent_id": None, "category": "N",
             "in_tapatalk": False}])
        db.upsert_many("forumer_topics", uniform([
            {"id": 5, "forum_id": 7, "title": "Kept", "description": "still here", "in_tapatalk": True},
            {"id": 9, "forum_id": 40, "title": "Lost comic", "description": "drawn", "in_tapatalk": False},
            {"id": 8, "forum_id": 19, "title": "Only listed", "description": None, "in_tapatalk": False}]))
        db.upsert_many("forumer_members", uniform([
            {"old_id": 1, "name": "bobby_shaftoe", "user_id": 9370595, "location": "Here",
             "group_name": "Members", "title": "Advanced Member",
             "country": "Australia", "msn": "bob@example.com", "website": "http://bob.example",
             "birthday": "1990-01-01", "interests": "N"},
            {"old_id": 2, "name": "Ghost", "user_id": None, "title": "Newbie", "post_count": 4,
             "signature": "<b>boo</b>"}]))
        db.upsert_many("forumer_posts", [
            {"id": 500, "topic_id": 5, "post_id": 10, "member_id": None, "author": "visitor",
             "posted_at": "2006-01-01 10:00:00", "html": "first"},
            {"id": 501, "topic_id": 9, "post_id": None, "member_id": 1, "author": "bobby_shaftoe",
             "posted_at": "2005-01-01 10:00:00", "html": "a <b>comic</b>"},
            {"id": 502, "topic_id": 9, "post_id": None, "member_id": 2, "author": "Ghost",
             "posted_at": "2005-01-02 10:00:00", "html": "boo"},
            {"id": 503, "topic_id": 9, "post_id": None, "member_id": None, "author": "passerby",
             "posted_at": "2005-01-03 10:00:00", "html": "hi"}])
        db.upsert_many("forumer_attachments", [{"old_post_id": 501, "ref": "501", "kind": "file",
                                                "name": "comic.png", "data": PNG}])
        db.upsert_many("forumer_avatars", [{"old_id": 1, "url": "http://x/av-1.png", "data": PNG}])
        counts = build_metanet_site(db, tmp_path, "Metanet Forums")
    assert counts == {"forums": 4, "topics": 3, "users": 2, "posts": 5}

    read = lambda path: (tmp_path / path).read_text(encoding="utf-8")
    community = read("f/7.html")
    assert '<a href="../f/40.html">N Webcomics</a>' in community
    assert "<th>Description</th>" in community and '<td class="description">still here</td>' in community
    assert 'visitor <span class="guest">(guest)</span>' in community  # who started topic 5
    assert '<a href="../f/19.html">N Images</a>' in read("f/64.html")  # in its category
    webcomics = read("f/40.html")
    assert "Lost comic" in webcomics and '<td class="description">drawn</td>' in webcomics
    assert "No posts of this topic were archived." in read("t/8.html")
    assert "Only listed</a></td><td class=\"description\"></td><td></td>" in read("f/19.html")  # starter unknown

    lost = read("t/9.html")
    assert lost.index('id="o501"') < lost.index('id="o502"') < lost.index('id="o503"')
    assert '<a class="id" href="../t/9.html#o501">#501 (forumer)</a>' in lost
    assert "a <b>comic</b>" in lost and "files/attachments/f501-0-comic.png" in lost
    assert '<a href="../u/9370595.html">bobby_shaftoe</a>' in lost  # the id-named account, named
    assert '<a href="../u/f2.html">Ghost</a>' in lost and 'passerby <span class="guest">(guest)</span>' in lost
    kept = read("t/5.html")
    assert '<a href="../t/9.html#o501">that</a>' in kept

    profile = read("u/9370595.html")
    assert ("<tr><th>ID</th><td>9,370,595</td></tr><tr><th>Forumer ID</th><td>1</td></tr>"
            "<tr><th>Name</th><td>bobby_shaftoe</td></tr>") in profile
    assert "<tr><th>Location</th><td>Here, Australia</td></tr>" in profile
    assert '<a href="http://bob.example">http://bob.example</a>' in profile
    assert "<tr><th>Socials</th><td>MSN: bob@example.com</td></tr>" in profile
    assert ("<tr><th>Old title</th><td>Advanced Member</td></tr>"
            "<tr><th>Main group</th><td>Members</td></tr>") in profile  # no Tapatalk rank nor groups
    assert "<th>Rank</th>" not in profile  # empty fields aren't shown
    assert profile.count('class="avatar"') == 2  # Tapatalk's and forumer's differ
    ghost = read("u/f2.html")
    assert "<th>ID</th>" not in ghost and "<tr><th>Forumer ID</th><td>2</td></tr>" in ghost
    assert "<tr><th>Old title</th><td>Newbie</td></tr>" in ghost and "<th>Rank</th>" not in ghost
    assert "<b>boo</b>" in ghost
    assert '<tr><th>Posts</th><td><a href="../u/f2-posts.html">4</a></td></tr>' in ghost
    assert 'id="o502"' in read("u/f2-posts.html")

    users = read("users.html")
    assert users.index("bobby_shaftoe") < users.index("Ghost")  # by id; those Tapatalk lacks last
    assert ('<tr><td class="id">1</td><td class="id">9,370,595</td>'
            '<td class="name"><a href="u/9370595.html">bobby_shaftoe</a></td>') in users
    assert '<td class="count"><a href="u/f2-posts.html">4</a></td>' in users
