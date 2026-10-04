from datetime import datetime
from email.message import Message

import pytest

from tapascrape.content import attachments as attachments_module
from tapascrape.content.attachments import (MISSING_PREFIX, Attachment, canonical,
                                            collect_attachments, looks_like_file, recover_attachments)
from tapascrape.content.fix import FixContext, fix_source
from tapascrape.db import open_database
from tapascrape.net.throttle import Throttle
from tapascrape.net.wayback import Capture

TXT = "http://2.forumer.com/uploads/metanet/post-10-1081445810.txt"
PNG = "http://2.forumer.com/uploads/metanet/post-1-1181593950.png"
ACT = "http://metanet.2.forumer.com/index.php?act=Attach&type=post&id=52852"
BLOCK = "--------------------[url=/attach/ma/post-10-1081445810.txt]Click here to view the attachment[/url]"


def test_canonical():
    assert canonical("/attach/ma/post-10-1081445810.txt") == Attachment(
        "upload", TXT, "post-10-1081445810.txt", 10, datetime(2004, 4, 8, 17, 36, 50))
    assert canonical("HTTP://2.Forumer.com:80//uploads/metanet/post-1-1181593950.png.") == canonical(PNG)
    assert canonical("http://metanet.2.forumer.com/index.php?s=abc&act=Attach&amp;type=post&id=52852") == \
        Attachment("attach-id", ACT, old_attach_id=52852)
    assert canonical("http://5.forumer.com/uploads/interim/av-3.gif").name == "av-3.gif"
    assert canonical("http://metanet.2.forumer.com/index.php?showtopic=5") is None


CTX = FixContext(attachments={TXT: 1, PNG: 2, ACT: 3})


@pytest.mark.parametrize("source, fixed", [
    (f"here is a little scene of it {BLOCK}", "here is a little scene of it [ts:attachment=1]"),
    (f"[img]'{PNG}'[/img]", "[ts:attachment-image=2]"),
    (f"[url={PNG}]my level[/url] and [url]{ACT}[/url]",
     f"[ts:attachment-link=2]my level[/ts:attachment-link] and [ts:attachment-link=3]{ACT}[/ts:attachment-link]"),
    (f"get it at {ACT}.", f"get it at [ts:attachment-link=3]{ACT}[/ts:attachment-link]."),
    # unknown files and code blocks stay as they are
    ("[img]http://2.forumer.com/uploads/metanet/other.png[/img]",
     "[img]http://2.forumer.com/uploads/metanet/other.png[/img]"),
    (f"[code][img]{PNG}[/img][/code]", f"[code][img]{PNG}[/img][/code]"),
])
def test_sentinels(source, fixed):
    assert fix_source(source, ctx=CTX)[0] == fixed


@pytest.fixture
def db():
    with open_database(":memory:") as db:
        db.create_schema()
        db.upsert_many("posts", [
            {"id": 5, "topic_id": 1, "index": 1, "content": "", "source": f"a {BLOCK}"},
            {"id": 3, "topic_id": 1, "index": 2, "content": "", "source": f"[img]'{PNG}'[/img] {ACT}"},
            {"id": 9, "topic_id": 1, "index": 3, "content": "", "source": f"again [url]{TXT}[/url]"},
        ])
        db.upsert_many("users", [{"id": 1, "name": "x", "signature": f'<img src="{PNG}"/>'}])
        yield db


def test_collect(db):
    assert collect_attachments(db) == 3
    assert db.query("SELECT id, kind, url, name, old_forum_id, old_attach_id, first_post_id, uses "
                    "FROM attachments ORDER BY id") == [
        (1, "upload", PNG, "post-1-1181593950.png", 1, None, 3, 2),
        (2, "attach-id", ACT, None, None, 52852, 3, 1),
        (3, "upload", TXT, "post-10-1081445810.txt", 10, None, 5, 2)]
    db.upsert_many("posts", [{"id": 1, "topic_id": 1, "index": 4, "content": "",
                              "source": "[img]http://2.forumer.com/uploads/metanet/new.png[/img]"}])
    assert collect_attachments(db) == 1
    assert db.query("SELECT id, name FROM attachments WHERE id = 4") == [(4, "new.png")]


def test_looks_like_file():
    assert looks_like_file(b"GIF89a...", "a.gif")
    assert not looks_like_file(b"<!DOCTYPE html><html>parked</html>", "a.gif")
    assert not looks_like_file(b"\n<!DOCTYPE html>parked", "level.txt")
    assert looks_like_file(b"$0^#N level data", "level.txt")
    assert looks_like_file(b"<html>a page</html>", "page.html")


def test_recover(db, monkeypatch):
    collect_attachments(db)
    parked = (b"<!DOCTYPE html><html>domain for sale</html>", Message())
    live_calls = []

    def fake_fetch(url, throttle, timeout=30, max_retries=3, original=False):
        live_calls.append(url)
        return parked

    headers = Message()
    headers["x-archive-orig-content-disposition"] = 'attachment; filename="level pack.zip"'
    archived = {"1": (b"\x89PNG\r\n\x1a\nimage", Message()), "2": (b"PK\x03\x04zip", headers),
                "3": parked}
    captures = {PNG: [Capture(PNG, "1", "image/png")], ACT: [Capture(ACT, "2", "application/zip")],
                TXT: [Capture(TXT, "3", "text/html")]}
    monkeypatch.setattr(attachments_module, "fetch", fake_fetch)
    monkeypatch.setattr(attachments_module, "wayback_index", lambda urls, throttle: captures)
    monkeypatch.setattr(attachments_module, "fetch_capture",
                        lambda c, throttle: (*archived[c.timestamp], f"wb/{c.timestamp}"))
    assert recover_attachments(db, Throttle(0), Throttle(0)) == 2
    assert live_calls == [PNG, ACT]  # each parked host is tried once, then given up on
    assert db.query("SELECT id, name, content_type, size, recovered_from FROM attachments "
                    "WHERE data IS NOT NULL ORDER BY id") == [
        (1, "post-1-1181593950.png", "image/png", 13, "wb/1"),
        (2, "level pack.zip", None, 7, "wb/2")]
    assert db.states(MISSING_PREFIX) == {"3": "archived copies aren't the file"}
    assert recover_attachments(db, Throttle(0), Throttle(0)) == 0


def test_error_pages_are_not_downloads():
    from tapascrape.content.attachments import valid_download
    error = Message()
    error["x-archive-orig-content-type"] = "text/html"
    assert not valid_download(b"Can't connect to local MySQL server through socket", error, None)
    download = Message()
    download["x-archive-orig-content-type"] = "text/plain"
    download["x-archive-orig-content-disposition"] = 'inline; filename="Minimalist_Series.txt"'
    assert valid_download(b"$level data", download, None)
