import io

import pytest

from tapascrape.content.scan import (ScanReport, matching_posts, print_report, scan, scan_text,
                                     tag_balance, unknown_tags)
from tapascrape.db import open_database

YUKU_POST = ("But then theres a [size=100][color=RED'>]*HEAD EXPLODES*[/color][/size] "
             "[img]http://static.yuku.com/domain/bypass/images/tongue.gif[/img]")
TABLE_QUOTE = ("[table][tr][td][b]QUOTE[/b] (Obby)[/td][/tr][tr][td]hi[/td][/tr][/table]\n"
               "[url='http://numa.notdot.net/map/2992']map[/url] [sarcasm]sure[/sarcasm]")


def hits(text: str) -> dict[str, int]:
    report = ScanReport()
    scan_text(report, 1, text)
    return {name: f.matches for name, f in report.rules.items() if f.matches}


def test_rules():
    assert hits(YUKU_POST) == {"attribute-leak": 1, "size-100": 1, "yuku-smiley": 1}
    assert hits(TABLE_QUOTE) == {"table-quote": 1, "quoted-url": 1}
    assert hits("[img]'http://2.forumer.com/html/emoticons/mellow.gif'[/img]<br>") == {
        "quoted-img": 1, "forumer-emoticon": 1, "html-br": 1}
    assert hits("plain [b]text[/b] with [url=http://x.org]a link[/url]") == {}


def test_tag_balance_and_unknown_tags():
    assert tag_balance("[b]x[/b] [i]y [list][*]a[*]b[/list] [/quote]") == {"i": 1, "quote": -1}
    assert unknown_tags(TABLE_QUOTE) == {"sarcasm"}


@pytest.fixture
def db():
    with open_database(":memory:") as db:
        db.create_schema()
        db.upsert_many("posts", [
            {"id": 1, "topic_id": 1, "index": 1, "content": "", "source": YUKU_POST},
            {"id": 2, "topic_id": 1, "index": 2, "content": "", "source": TABLE_QUOTE},
            {"id": 3, "topic_id": 1, "index": 3, "content": "<b>no source</b>", "source": None},
        ])
        yield db


def test_scan_database(db):
    report = scan(db)
    assert report.posts == 2
    assert report.rules["attribute-leak"].posts == 1
    assert report.rules["attribute-leak"].examples[0][0] == 1
    assert report.unknown_tags == {"sarcasm": 1}
    assert list(matching_posts(db, "table-quote")) == [2]
    out = io.StringIO()
    print_report(report, out)
    assert "Scanned 2 post sources." in out.getvalue()
