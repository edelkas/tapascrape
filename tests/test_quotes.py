from datetime import datetime

import pytest

from tapascrape.content.quotes import QuoteDate, link_quotes, own_text, parse_quotes, tz_offset
from tapascrape.db import open_database


def test_parse_nesting():
    text = ('[quote="ZZ9" date="June 26, 2006 11:06 am"]hi [quote=brickman @ Jan 26 2005, 10:02 PM]'
            'inner[/quote] there[/quote] mine [code][quote]not one[/quote][/code][quote]open')
    quotes = parse_quotes(text)
    assert [(q.position, q.level, q.parent, q.author, q.body) for q in quotes] == [
        (0, 1, None, "ZZ9", "hi   there"), (1, 2, 0, "brickman", "inner"), (2, 1, None, None, "open")]
    assert quotes[0].when == QuoteDate(2006, 6, 26, 11, 6)
    assert quotes[1].when == QuoteDate(2005, 1, 26, 22, 2)
    # the unclosed quote is kept: it may well be the post's own text
    assert own_text(text, quotes).split() == ["mine", "[code][quote]not", "one[/quote][/code][quote]open"]


@pytest.mark.parametrize("tag, author, date, when", [
    ('[quote="DemonzLunchBreak @  25, 2007 03:11 pm"]', "DemonzLunchBreak", "25, 2007 03:11 pm",
     QuoteDate(2007, None, 25, 15, 11)),
    ('[quote="Harley @ October 30 @  2006 06:56 am"]', "Harley", "October 30 @  2006 06:56 am",
     QuoteDate(2006, 10, 30, 6, 56)),
    ("[QUOTE=Daddaluma,March 08, 2006 11:00 pm]", "Daddaluma", "March 08, 2006 11:00 pm",
     QuoteDate(2006, 3, 8, 23, 0)),
    ('[quote="(ZZ9 @ September 17 @\xa0 2006 05:29 pm)"]', "ZZ9", "September 17 @\xa0 2006 05:29 pm",
     QuoteDate(2006, 9, 17, 17, 29)),
    ('[quote="IAABH Posted on February 18 @  2007 12:00 am"]', "IAABH", "February 18 @  2007 12:00 am",
     QuoteDate(2007, 2, 18, 0, 0)),
    ('[quote="@ March 05, 2008 04:05 pm"]', None, "March 05, 2008 04:05 pm", QuoteDate(2008, 3, 5, 16, 5)),
    ('[quote="macaddict_17 @  January 31, 2007"]', "macaddict_17", "January 31, 2007",
     QuoteDate(2007, 1, 31, None, None)),
    ('[quote="\\"-Slayer-\\""]', '"-Slayer-"', None, None),
    ('[quote="Isaiah 40:22"]', "Isaiah 40:22", None, None),
    ("[quote]", None, None, None),
])
def test_attribution(tag, author, date, when):
    quote, = parse_quotes(tag + "x[/quote]")
    assert (quote.author, quote.date, quote.when) == (author, date, when)


def test_explicit_ids():
    tapatalk, xenforo, ipb = parse_quotes('[quote uid=9375890 name="Pikman" ]a[/quote]'
                                          '[QUOTE="Bob, post: 123, member: 45"]b[/QUOTE]'
                                          "[quote name='a' timestamp='1161298961' post='77']c[/quote]")
    assert (tapatalk.author, tapatalk.user_ref, tapatalk.post_ref) == ("Pikman", 9375890, None)
    assert (xenforo.author, xenforo.user_ref, xenforo.post_ref) == ("Bob", 45, 123)
    assert (ipb.author, ipb.post_ref, ipb.when) == ("a", 77, QuoteDate(2006, 10, 19, 23, 2, utc=True))
    guest, = parse_quotes("[quote uid=0 name=x]y[/quote]")
    assert guest.user_ref is None


def test_tz_offset():
    posted = datetime(2006, 6, 26, 16, 6, 40)
    assert tz_offset(QuoteDate(2006, 6, 26, 16, 6), posted) == 0
    assert tz_offset(QuoteDate(2006, 6, 26, 11, 6), posted) == -300
    assert tz_offset(QuoteDate(2006, 6, 26, 21, 37), posted) == 330  # +5:30, a minute off
    assert tz_offset(QuoteDate(2006, 6, 26, 11, 13), posted) is None
    assert tz_offset(QuoteDate(2006, None, 26, 11, 6), posted) == -300
    assert tz_offset(QuoteDate(2006, 6, 26, None, None), posted) == 0


@pytest.fixture
def db():
    with open_database(":memory:") as db:
        db.create_schema()
        yield db


ORIGINAL = "The quick brown fox jumps over the lazy dog and then runs far away into the woods"


def test_link_quotes(db):
    db.upsert_many("users", [{"id": 1, "name": "Keron Cyst"}, {"id": 2, "name": "ska"},
                             {"id": 3, "name": "9370595"}])
    posts = [
        (10, 1, "2006-06-26 16:06:40", ORIGINAL),
        (11, 2, "2006-06-26 17:00:00", "Something else entirely, nothing like it at all here"),
        (12, 3, "2006-06-26 18:00:00", "[quote=\"Keron Cyst\" date=\"June 26, 2006 11:06 am\"]"
                                       "The quick brown fox[/quote] yes"),           # author+date (-5h)
        (13, 2, "2006-06-26 19:00:00", f"[quote]{ORIGINAL}[/quote] indeed"),       # text
        (14, 1, "2006-06-26 20:00:00", "[quote=bobby_shaftoe]me![/quote][quote=ska]short[/quote]"),
        (15, 2, "2006-06-26 21:00:00", "[quote=\"9370595\" date=\"June 26, 2006 01:00 pm\"]"
                                       "[quote=\"Keron Cyst\" date=\"June 26, 2006 11:06 am\"]brown fox"
                                       "[/quote]yes[/quote]"),                       # nested
        (16, 2, "2006-06-26 22:00:00", "[quote=nobody]words that were never written by anyone[/quote]"),
    ]
    db.upsert_many("posts", [{"id": i, "topic_id": 5, "user_id": u, "index": i, "timestamp": t,
                              "content": "", "source": s} for i, u, t, s in posts])
    report = link_quotes(db, aliases={3: {"bobby_shaftoe"}})
    rows = db.query("SELECT post_id, position, level, parent_position, quoted_post_id, quoted_user_id, "
                    "match, tz_offset FROM quotes ORDER BY post_id, position")
    assert rows == [
        (12, 0, 1, None, 10, 1, "author+date", -300),
        (13, 0, 1, None, 10, 1, "text", None),
        (14, 0, 1, None, 12, 3, "author", None),
        (14, 1, 1, None, 13, 2, "author", None),
        (15, 0, 1, None, 12, 3, "author+date", -300),
        (15, 1, 2, 0, 10, 1, "author+date", -300),
        (16, 0, 1, None, None, None, None, None),
    ]
    assert report["quotes"] == 7 and report["unmatched"] == 1
    link_quotes(db, aliases={3: {"bobby_shaftoe"}})  # rebuilt, not duplicated
    assert db.query("SELECT COUNT(*) FROM quotes") == [(7,)]
