import pytest

from tapascrape.content.fix import FIXES_BY_NAME, FixContext, fix_posts, fix_source, quote_open
from tapascrape.db import open_database


def q(author: str, body: str) -> str:
    """A migrated quote table, as Yuku left them."""
    return f"[table][tr][td][b]QUOTE[/b] {author}[/td][/tr][tr][td]{body}[/td][/tr][/table]"


@pytest.mark.parametrize("source, fixed", [
    ("[color=RED'>]Red[/color] [font=COMIC SANS MS'>]x[/font] [spoiler=Hi'>]s[/spoiler]",
     "[color=RED]Red[/color] [font=COMIC SANS MS]x[/font] [spoiler=Hi]s[/spoiler]"),
    ("[url='http://numa.notdot.net/map/2992']map[/url]",
     "[url=http://numa.notdot.net/map/2992]map[/url]"),
    ("[img]'http://2.forumer.com/uploads/metanet/post-1.png'[/img]",
     "[img]http://2.forumer.com/uploads/metanet/post-1.png[/img]"),
    # [size=100] pairs go, other sizes stay, even when nested
    ("[size=100][size=7]big[/size] [b]x[/b][/size] [size=100]unclosed",
     "[size=7]big[/size] [b]x[/b] [size=100]unclosed"),
    # quote tables, nested, with or without author and date
    (q("(brickman @ Nov 4 2005, 11:47 PM)", "\n" + q("(kyubbi238)", "hi") + "\nwow") + " \nyes",
     '[quote="brickman" date="Nov 4 2005, 11:47 PM"]\n[quote="kyubbi238"]hi[/quote]\nwow[/quote]'
     " \nyes"),
    (q("", "Fake tiles"), "[quote]Fake tiles[/quote]"),
    ("[table][tr][td][b]CODE[/b] [/td][/tr][tr][td][quote]typed[/quote][/td][/tr][/table]",
     "[code][quote]typed[/quote][/code]"),
    # tables that aren't a single quote/code block, or are unclosed, stay as they are
    ("[table][tr][td]a[/td][td]b[/td][/tr][/table]", "[table][tr][td]a[/td][td]b[/td][/tr][/table]"),
    ("[table][tr][td][b]QUOTE[/b] [/td][/tr][tr][td]never closed",
     "[table][tr][td][b]QUOTE[/b] [/td][/tr][tr][td]never closed"),
    ("plain [b]text[/b] [sarcasm]sure[/sarcasm]", "plain [b]text[/b] [sarcasm]sure[/sarcasm]"),
])
def test_fix_source(source, fixed):
    assert fix_source(source)[0] == fixed


@pytest.mark.parametrize("header, tag", [
    (" ", "[quote]"),
    (" (me)", '[quote="me"]'),
    (" (trib4lmaniac @ Feb 23 2005, 07:28 AM)", '[quote="trib4lmaniac" date="Feb 23 2005, 07:28 AM"]'),
    (" (ZZ9 @ October 03, 2006 05:34 pm)", '[quote="ZZ9" date="October 03, 2006 05:34 pm"]'),
    (" (Keron Cyst @ May 2 2004 @\xa0 03:59 AM)", '[quote="Keron Cyst" date="May 2 2004 @\xa0 03:59 AM"]'),
    (" (tktktk @ Jan 4 2005, 02:06 AM (slightly edited))",
     '[quote="tktktk @ Jan 4 2005, 02:06 AM (slightly edited)"]'),
    (' (the "boss")', '[quote="the \\"boss\\""]'),
])
def test_quote_header(header, tag):
    assert quote_open(header) == tag


def test_fix_reports_applied_fixes():
    assert fix_source("[url='x']a[/url] [size=100]b[/size]") == ("[url=x]a[/url] b",
                                                                  ["quoted-url", "size-100"])
    assert fix_source("[url='x']a[/url]", [FIXES_BY_NAME["size-100"]]) == ("[url='x']a[/url]", [])


def test_fix_posts():
    with open_database(":memory:") as db:
        db.create_schema()
        db.upsert_many("posts", [
            {"id": 1, "topic_id": 1, "index": 1, "content": "", "source": "[url='x']a[/url]"},
            {"id": 2, "topic_id": 1, "index": 2, "content": "", "source": "fine"},
            {"id": 3, "topic_id": 1, "index": 3, "content": "html only", "source": None},
        ])
        changed = fix_posts(db)
        assert changed["quoted-url"] == 1 and changed["(any)"] == 1
        assert db.query("SELECT id, source, source_fixed FROM posts ORDER BY id") == [
            (1, "[url='x']a[/url]", "[url=x]a[/url]"), (2, "fine", "fine"), (3, None, None)]


SMILEY = "http://static.yuku.com/domain/bypass/images/tongue.gif"
CTX = FixContext(smilies={SMILEY: 7, "http://2.forumer.com/html/emoticons/metanet/;(.gif": 8},
                 topics={996, 2592}, forums={39})
OLD = "http://metanet.2.forumer.com/index.php"


@pytest.mark.parametrize("source, fixed", [
    # smileys, quoted or not, any host spelling; unknown images stay
    (f"hi [img]{SMILEY}[/img] [img]'Http://2.forumer.com/html/emoticons/metanet/;(.gif'[/img]",
     "hi [ts:smiley=7] [ts:smiley=8]"),
    ("[img]http://2.forumer.com/html/emoticons/metanet/unknown.gif[/img]",
     "[img]http://2.forumer.com/html/emoticons/metanet/unknown.gif[/img]"),
    # ...but not inside code blocks
    (f"[code][img]{SMILEY}[/img][/code] [img]{SMILEY}[/img]",
     f"[code][img]{SMILEY}[/img][/code] [ts:smiley=7]"),
    # topic links: named, autolinked, bare (punctuation stays outside), with page and post
    (f"[url='{OLD}?showtopic=996']here[/url]", "[ts:topic=996]here[/ts:topic]"),
    (f"[url]{OLD}?showtopic=996&st=20[/url]",
     f"[ts:topic=996 start=20]{OLD}?showtopic=996&st=20[/ts:topic]"),
    (f"see {OLD}?showtopic=2592&view=findpost&p=43596.",
     f"see [ts:topic=2592 old_post=43596]{OLD}?showtopic=2592&view=findpost&p=43596[/ts:topic]."),
    (f"[url={OLD}?act=ST&f=5&t=996&st=0#entry71731]x[/url]",
     "[ts:topic=996 old_post=71731]x[/ts:topic]"),
    (f"[url={OLD}?showforum=39]The Legacy[/url]", "[ts:forum=39]The Legacy[/ts:forum]"),
    # unknown topics, other pages and truncated URLs stay as they are
    (f"[url={OLD}?showtopic=5]x[/url] [url={OLD}?showuser=438]u[/url] {OLD}?sho...ndpost&p=1",
     f"[url={OLD}?showtopic=5]x[/url] [url={OLD}?showuser=438]u[/url] {OLD}?sho...ndpost&p=1"),
    # an old-board URL as the text of another link isn't touched
    (f"[url=http://example.com]{OLD}?showtopic=996[/url]",
     f"[url=http://example.com]{OLD}?showtopic=996[/url]"),
])
def test_sentinels(source, fixed):
    assert fix_source(source, ctx=CTX)[0] == fixed


def test_no_sentinels_in_posts_that_use_the_namespace():
    source = f"[ts:smiley=1] is how I'd write it [img]{SMILEY}[/img] [url='{OLD}?showtopic=996']x[/url]"
    assert fix_source(source, ctx=CTX) == (
        f"[ts:smiley=1] is how I'd write it [img]{SMILEY}[/img] [url={OLD}?showtopic=996]x[/url]",
        ["quoted-url"])


def test_fix_signatures():
    from tapascrape.content.fix import fix_signatures
    with open_database(":memory:") as db:
        db.create_schema()
        db.upsert_many("smilies", [{"id": 4, "url": "https://www.tapatalk.com/groups/metanetfr/forum_data/"
                                                    "forums.me/meta/metanetfr/smilies/4.gif"}])
        db.upsert_many("topics", [{"id": 996, "forum_id": 1, "name": "t", "stickied": False,
                                   "locked": False}])
        db.upsert_many("users", [
            {"id": 1, "name": "Keron Cyst", "signature": 'loLol<br/><img class="postimage" '
             'src="forum_data/forums.me/meta/metanetfr/smilies/4.gif"/>'},
            {"id": 2, "name": "b", "signature": '<a class="postlink" href="http://metanet.2.forumer.com/'
             'index.php?showtopic=996">my thread</a>'},
            {"id": 3, "name": "c", "signature": None},
        ])
        changed = fix_signatures(db)
        assert changed["smilies"] == 1 and changed["old-links"] == 1
        assert db.query("SELECT id, signature_source, signature_fixed FROM users ORDER BY id") == [
            (1, "loLol\n[img]forum_data/forums.me/meta/metanetfr/smilies/4.gif[/img]",
             "loLol\n[ts:smiley=4]"),
            (2, "[url=http://metanet.2.forumer.com/index.php?showtopic=996]my thread[/url]",
             "[ts:topic=996]my thread[/ts:topic]"),
            (3, None, None)]
