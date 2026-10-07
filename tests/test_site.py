import pytest

from tapascrape.db import open_database
from tapascrape.site.build import build_site
from tapascrape.site.render import Linked, Links, Quoted, Renderer

GIF = b"GIF89a\x01\x00\x01\x00\x00\x00\x00;"


@pytest.mark.parametrize("bbcode, expected", [
    ("[b]bold [i]both[/b] after[/i]", "<b>bold <i>both</i></b> after[/i]"),
    ("a & <b> &nbsp; &#153;", "a &amp; &lt;b&gt; &nbsp; &#153;"),
    ("see http://x.com/a.", 'see <a href="http://x.com/a">http://x.com/a</a>.'),
    ("one\ntwo", "one<br>\ntwo"),
    ("[list=1]\n[*]one\n[*]two\n[/list]\nafter", "<ol><li>one</li><li>two</li></ol>after"),
    ("[list][*]a[/list]", "<ul><li>a</li></ul>"),
    ("[color=RED'>]x[/color] [color=red]y[/color]", "[color=RED'&gt;]x[/color] <span style=\"color:red\">y</span>"),
    ("[size=14]z[/size] [size=2]w[/size]", '<span style="font-size:14px">z</span> <span style="font-size:82%">w</span>'),
    ("[sarcasm]no[/sarcasm] [b]unclosed", "[sarcasm]no[/sarcasm] [b]unclosed"),
    ("[code][b]literal[/b] <x>[/code]", '<pre class="code">[b]literal[/b] &lt;x&gt;</pre>'),
    ("[spoiler=Title]s[/spoiler]", '<details class="spoiler"><summary>Spoiler: Title</summary>s</details>'),
    ("[url=javascript:alert(1)]bad[/url]", "[url=javascript:alert(1)]bad[/url]"),
    ("[url]www.a.com[/url] [url=http://a.com]a [b]b[/b][/url]",
     '<a href="http://www.a.com">www.a.com</a> <a href="http://a.com">a <b>b</b></a>'),
    ("[img]http://a.com/x.png[/img]", '<img class="bb-img" src="http://a.com/x.png" alt="" loading="lazy">'),
    ("[table][tr]\n[td]a[/td]\n[/tr][/table]", '<table class="bb-table"><tr><td>a</td></tr></table>'),
    ("[align=center][b]Soar.[/b][/align]\nx [center]c[/center] [align=up]u[/align]",
     '<div class="align-center"><b>Soar.</b></div>x <div class="align-center">c</div> [align=up]u[/align]'),
])
def test_render(bbcode, expected):
    assert Renderer().render(bbcode) == expected


class FakeLinks(Links):
    def smiley(self, smiley_id):
        return Linked(f"files/smilies/{smiley_id}.gif", "yay", True)

    def attachment(self, attachment_id):
        return (Linked(f"files/attachments/{attachment_id}-a.zip", "a.zip") if attachment_id == 7
                else Linked(None, "b.zip") if attachment_id == 8 else None)

    def topic(self, topic_id, attrs):
        return f"t/{topic_id}.html"

    def quoted(self, post_id, position):
        return Quoted("t/5.html#p10", "Keron Cyst", "2006-06-26T16:06:00Z") if post_id == 12 else None


def test_render_sentinels_and_quotes():
    r = Renderer(FakeLinks())
    assert r.render("[ts:smiley=3] [ts:topic=996 start=20]here[/ts:topic]", "../") == (
        '<img class="smiley" src="../files/smilies/3.gif" alt="yay" title="yay"> '
        '<a href="../t/996.html">here</a>')
    assert r.render("[ts:attachment=7][ts:attachment=8][ts:attachment=9]") == (
        '<div class="attachment">Attachment: <a href="files/attachments/7-a.zip">a.zip</a></div>'
        '<div class="attachment attachment-missing">Attachment (lost): b.zip</div>'
        '<div class="attachment attachment-missing">Attachment (lost)</div>')
    html = r.render('[quote]x[/quote][code][quote][/code][quote="Keron Cyst" date="June 26, 2006 11:06 am"]\n'
                    "y\n[/quote]\nz", "../", post_id=12)
    # the tag's attribution as written; what a bare one lacks, from the quoted post
    assert html == ('<blockquote class="quote"><div class="quote-head"><a href="../t/5.html#p10">'
                    "Quote: Keron Cyst, 2006-06-26T16:06:00Z</a></div>x</blockquote>"
                    '<pre class="code">[quote]</pre>'
                    '<blockquote class="quote"><div class="quote-head"><a href="../t/5.html#p10">'
                    "Quote: Keron Cyst, June 26, 2006 11:06 am</a></div>y</blockquote>z")
    assert Renderer().render("[quote=x]y[/quote]") == (
        '<blockquote class="quote"><div class="quote-head">Quote: x</div>y</blockquote>')


def test_build_site(tmp_path):
    with open_database(":memory:") as db:
        db.create_schema()
        db.upsert_many("forums", [{"id": 1, "parent_id": None, "name": "Category", "last_post_id": None},
                                  {"id": 2, "parent_id": 1, "name": "Old", "last_post_id": 10},
                                  {"id": 3, "parent_id": 1, "name": "New", "last_post_id": 13}])
        db.upsert_many("topics", [
            {"id": 5, "forum_id": 2, "user_id": 1, "name": "First", "stickied": False, "locked": True,
             "created_at": "2006-01-01 10:00:00", "post_count": 2, "view_count": 9, "last_post_id": 11},
            {"id": 6, "forum_id": 3, "user_id": 2, "name": "Recent", "stickied": False, "locked": False,
             "created_at": "2007-01-01 10:00:00", "post_count": 1, "view_count": 1, "last_post_id": 13},
            {"id": 7, "forum_id": 3, "user_id": 2, "name": "Sticky <old>", "stickied": True, "locked": False,
             "created_at": "2005-01-01 10:00:00", "post_count": 1, "view_count": 1, "last_post_id": 12}])
        db.upsert_many("posts", [
            {"id": 10, "topic_id": 5, "user_id": 1, "index": 1, "timestamp": "2006-01-01 10:00:00",
             "content": "", "source": "x", "source_fixed": "Hello [ts:smiley=1]"},
            {"id": 11, "topic_id": 5, "user_id": 0, "index": 2, "timestamp": "2006-01-02 10:00:00",
             "content": "", "source": "x", "source_fixed": "[quote=alice]Hello[/quote] hi"},
            {"id": 12, "topic_id": 7, "user_id": 2, "index": 1, "timestamp": "2005-01-01 10:00:00",
             "content": "", "source": "x", "source_fixed": "pinned"},
            {"id": 13, "topic_id": 6, "user_id": 2, "index": 1, "timestamp": "2007-01-01 10:00:00",
             "content": "<b>html</b> only", "source": None, "source_fixed": None}])
        db.upsert_many("users", [{"id": 1, "name": "alice", "post_count": 1, "signature": "<i>sig</i>",
                                  "joined_at": "2005-06-01 00:00:00", "rank": "Newbie"},
                                 {"id": 2, "name": "bob", "post_count": 2, "signature": None, "joined_at": None,
                                  "rank": None}])
        db.upsert_many("groups", [{"id": 2, "name": "Registered users"}, {"id": 5, "name": "Admins"}])
        db.upsert_many("group_users", [{"group_id": 5, "user_id": 1}, {"group_id": 2, "user_id": 1}])
        db.upsert_many("smilies", [{"id": 1, "url": "http://x/yay.gif", "name": "yay", "data": GIF}])
        db.upsert_many("quotes", [{"post_id": 11, "position": 0, "level": 1, "quoted_post_id": 10}])
        counts = build_site(db, tmp_path, "My Board")
    assert counts == {"forums": 3, "topics": 3, "users": 2, "posts": 4}
    index = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert '<link rel="stylesheet" href="style.css">' in index and (tmp_path / "style.css").exists()
    # the tree, each level latest first, nested levels indented
    assert index.index('href="f/1.html"') < index.index('href="f/3.html"') < index.index('href="f/2.html"')
    assert '<span class="indent"></span><a href="f/3.html">New</a>' in index
    forum = (tmp_path / "f" / "3.html").read_text(encoding="utf-8")
    assert forum.index("t/7.html") < forum.index("t/6.html")  # stickies first
    assert "Sticky &lt;old&gt;" in forum and '<td class="flags">S&nbsp;</td>' in forum
    assert '<a href="../index.html">Index</a> &rsaquo; <a href="../f/1.html">Category</a> &rsaquo; New' in forum
    topic = (tmp_path / "t" / "5.html").read_text(encoding="utf-8")
    assert topic.index('id="p10"') < topic.index('id="p11"')
    assert '<img class="smiley" src="../files/smilies/1.gif"' in topic
    assert '<a href="../t/5.html#p10">Quote: alice, 2006-01-01T10:00:00Z</a>' in topic
    assert '<span class="author">Guest</span>' in topic
    assert "2006-01-01T10:00:00Z" in topic
    assert "<b>html</b> only" in (tmp_path / "t" / "6.html").read_text(encoding="utf-8")
    user = (tmp_path / "u" / "1.html").read_text(encoding="utf-8")
    assert '<a href="../t/5.html#p10">First</a>' in user and '<i>sig</i>' in user
    assert "2005-06-01T00:00:00Z" in user
    assert '<nav class="shortcuts"><a href="../users.html">Users</a></nav>' in user
    assert '<a href="../users.html">Users</a> &rsaquo; alice' in user
    assert '<tr><th>Posts</th><td><a href="../u/1-posts.html">1</a></td></tr>' in user
    posts = (tmp_path / "u" / "1-posts.html").read_text(encoding="utf-8")
    assert 'id="p10"' in posts and "Hello" in posts and 'id="p11"' not in posts
    assert '&rsaquo; <a href="../u/1.html">alice</a> &rsaquo; Posts' in posts
    users = (tmp_path / "users.html").read_text(encoding="utf-8")
    assert users.index('href="u/1.html"') < users.index('href="u/2.html"')
    assert ('<tr><td class="id">1</td><td class="name"><a href="u/1.html">alice</a></td><td>Newbie</td>'
            '<td class="date">2005-06-01T00:00:00Z</td><td class="date"></td>'
            '<td class="count"><a href="u/1-posts.html">1</a></td>'
            '<td class="date"><a href="t/5.html#p10">2006-01-01T10:00:00Z</a></td>'
            '<td class="date"><a href="t/5.html#p10">2006-01-01T10:00:00Z</a></td></tr>') in users
    assert ("<tr><th>Rank</th><td>Newbie</td></tr><tr><th>Groups</th><td>Registered users, Admins</td></tr>"
            in user)


def test_render_deep_nesting():
    html = Renderer().render("[quote]" * 300 + "x" + "[/quote]" * 300)
    assert html.count("<blockquote") == 250 and "x" in html
