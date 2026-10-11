from datetime import datetime
from email.message import Message

import pytest

from tapascrape.content import images as images_module
from tapascrape.content.images import (archived_placeholders, collect_images, embedded_urls, image_url,
                                       placeholders, recover_images, whole_image)
from tapascrape.db import open_database
from tapascrape.net.throttle import RetryableError, Throttle
from tapascrape.net.wayback import payload_digest
from tapascrape.site.build import build_site
from tapascrape.site.render import Links, Renderer


def png(body: bytes) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + body + b"\x00\x00\x00\x00IEND\xaeB`\x82"


def gif(body: bytes) -> bytes:
    return b"GIF89a" + body + b";"


def test_image_url():
    assert image_url(" HTTP://IMG148.ImageShack.us:80/img148/6710/alumni.png#x ") == \
        "http://img148.imageshack.us/img148/6710/alumni.png"
    assert image_url("'www.a.com/b c.gif?x=1&amp;y=2'") == "http://www.a.com/b c.gif?x=1&y=2"
    assert image_url("/forum_data/smile.gif") is None
    assert image_url("javascript:alert(1)") is None


def test_embedded_urls():
    text = ("[url=http://x.org][img]http://i37.tinypic.com/a.gif[/img][/url] "
            "[IMG]http://2.forumer.com/html/emoticons/smile.gif[/IMG] "  # a smiley
            "[img]http://2.forumer.com/uploads/metanet/post-1-1181593950.png[/img] "  # an upload
            "[img=100x20]https://a.b/c.png[/img] [img]not a url[/img]")
    assert list(embedded_urls(text)) == ["http://i37.tinypic.com/a.gif", "https://a.b/c.png"]


def test_whole_image():
    assert whole_image(png(b"data")) == "image/png"
    assert whole_image(png(b"data")[:-12]) is None  # cut short
    assert whole_image(gif(b"data") + b"\x00\x00") == "image/gif"
    assert whole_image(b"GIF89a" + b"data") is None
    assert whole_image(b"\xff\xd8\xff\xe0data\xff\xd9") == "image/jpeg"
    assert whole_image(b"<html>not found</html>") is None


def test_placeholders():
    served = [("http://h.com/a.png", "P"), ("http://h.com/b.png", "P"), ("http://h.com/c.gif", "P"),
              # the same image uploaded twice under one name isn't a placeholder
              ("http://h.com/u1/sig.png", "S"), ("http://h.com/u2/sig.png", "S"),
              ("http://h.com/d.png", "D")]
    assert placeholders(served) == {"P"}
    captures = {"http://h.com/a.png": [["2006", "", "A"], ["2012", "", "P"]],
                "http://h.com/b.png": [["2012", "", "P"]],
                "http://h.com/c.png": [["2012", "", "P"]],
                # a userbar three members uploaded: theirs from the start
                "http://h.com/x.png": [["2006", "", "U"]], "http://h.com/y.png": [["2006", "", "U"]],
                "http://h.com/z.png": [["2007", "", "U"], ["2008", "", "Z"]]}
    assert archived_placeholders(captures) == {"P"}


def test_collect_images():
    with open_database(":memory:") as db:
        db.create_schema()
        assert collect_images(db, {"signatures": ["[img]http://a.com/1.png[/img]",
                                                  "[img]http://a.com/2.png[/img][img]http://a.com/2.png[/img]"]}) == 2
        assert db.query("SELECT id, url, host, uses, used_in FROM embedded_images ORDER BY id") == [
            (1, "http://a.com/2.png", "a.com", 2, "signatures"), (2, "http://a.com/1.png", "a.com", 1, "signatures")]
        assert collect_images(db, {"signatures": ["[img]http://a.com/3.png[/img] [img]http://A.com/1.png[/img]"]}) == 1
        assert db.query("SELECT id, url, uses, used_in FROM embedded_images ORDER BY id") == [
            (1, "http://a.com/2.png", 0, None), (2, "http://a.com/1.png", 1, "signatures"),
            (3, "http://a.com/3.png", 1, "signatures")]


ORIGINAL, WATERMARKED, PLACEHOLDER, LATER = png(b"original"), png(b"watermarked"), gif(b"removed"), png(b"v2")
URLS = {
    "same": "http://i1.pics.com/u/same.png",              # live, as archived
    "watermarked": "http://i2.pics.com/u/watermarked.png",  # live, but altered since
    "pruned": "http://img1.shack.us/pruned.png",          # 404; archived, then a placeholder
    "moved": "http://img1.shack.us/moved.png",            # redirected to the host's "lost" picture
    "relocated": "http://fc01.art.com/f/2007/my_sig.png",  # moved for real, to a CDN
    "gone 1": "http://pics.org/a.png",                     # both redirected to the same picture
    "gone 2": "http://pics.org/b.png",
    "unarchived": "http://i3.pics.com/u/unarchived.png",  # live, never archived
    "stand-in 1": "http://i4.pics.com/a/1.png",            # the same picture for three names:
    "stand-in 2": "http://i4.pics.com/a/2.png",            # a placeholder
    "stand-in 3": "http://i4.pics.com/a/3.gif",
    "cut": "http://old.host.com/cut.png",                  # gone; first capture incomplete
    "nowhere": "http://old.host.com/nowhere.gif",
    "spaced": "http://i5.pics.com/My pics/sig.png",
}


@pytest.fixture
def network(monkeypatch):
    calls = {"live": [], "archive": []}

    def live(url, throttle, timeout=30.0, max_retries=3):
        calls["live"].append(url)
        if "old.host.com" in url:
            raise RetryableError("URLError(gaierror(11001, 'getaddrinfo failed'))")
        answers = {
            URLS["same"]: ORIGINAL, URLS["watermarked"]: WATERMARKED, URLS["unarchived"]: LATER,
            URLS["stand-in 1"]: PLACEHOLDER, URLS["stand-in 2"]: PLACEHOLDER, URLS["stand-in 3"]: PLACEHOLDER,
            "http://i5.pics.com/My%20pics/sig.png": png(b"spaced")}
        if url == URLS["moved"]:
            return gif(b"lost"), Message(), "https://shack.us/lost.gif"
        if url == URLS["relocated"]:
            return png(b"sig"), Message(), "https://cdn.art.com/f/ae36/dzniqp-b48f.png?token=x"
        if url in (URLS["gone 1"], URLS["gone 2"]):
            return png(b"sorry"), Message(), "http://pics.org/static/sorry.png"
        if url in answers:
            return answers[url], Message(), url.replace("http:", "https:")
        return None

    archive = {
        URLS["same"]: [("20060101000000", ORIGINAL)],
        URLS["watermarked"]: [("20070101000000", ORIGINAL), ("20200101000000", WATERMARKED)],
        URLS["pruned"]: [("20060101000000", ORIGINAL), ("20130101000000", PLACEHOLDER)],
        URLS["stand-in 1"]: [("20060101000000", png(b"one")), ("20140101000000", PLACEHOLDER)],
        URLS["stand-in 2"]: [("20140101000000", PLACEHOLDER)],
        URLS["stand-in 3"]: [("20140101000000", PLACEHOLDER)],
        URLS["cut"]: [("20050101000000", png(b"cut")[:-12]), ("20060101000000", png(b"cut"))],
    }

    def captures(url, throttle):
        calls["archive"].append(url)
        return [images_module.Capture(url, ts, "image/png", payload_digest(data)) for ts, data in archive.get(url, [])]

    def fetch_capture(capture, throttle):
        calls["archive"].append(capture.timestamp)
        data = dict(archive[capture.original])[capture.timestamp]
        return data, Message(), f"https://web.archive.org/web/{capture.timestamp}id_/{capture.original}"

    monkeypatch.setattr(images_module, "fetch_final", live)
    monkeypatch.setattr(images_module, "image_captures", captures)
    monkeypatch.setattr(images_module, "fetch_capture", fetch_capture)
    monkeypatch.setattr(images_module, "LIVE_RATE", 1000.0)
    monkeypatch.setattr(images_module, "LIVE_WORKERS", 1)  # in order: a gone host is found first
    return calls


def test_recover_images(network):
    with open_database(":memory:") as db:
        db.create_schema()
        collect_images(db, {"signatures": [f"[img]{url}[/img]" for url in URLS.values()]})
        # a first run cut short: the rest is looked for next time
        report = recover_images(db, Throttle(0), limit=2)
        assert sum(report.values()) == 2
        report = recover_images(db, Throttle(0))
        assert sum(report.values()) == len(URLS) - 2
        rows = {url: tuple(row) for url, *row in db.query(
            "SELECT url, status, note, data, recovered_from, captured_at, live FROM embedded_images")}

        def row(name):
            return rows[URLS[name]]

        assert row("same")[:4] == ("live", "the same as the Wayback Machine's oldest copy", ORIGINAL, URLS["same"])
        assert row("watermarked")[:3] == ("archived", "the original host's copy has changed since", ORIGINAL)
        assert row("watermarked")[3] == f"https://web.archive.org/web/20070101000000id_/{URLS['watermarked']}"
        assert row("watermarked")[4] in (datetime(2007, 1, 1), "2007-01-01 00:00:00")
        assert row("pruned")[:3] == ("archived", "original host: not found (HTTP 4xx)", ORIGINAL)
        assert row("moved")[:2] == ("missing", "original host: a placeholder; not archived")
        assert row("moved")[5] == "moved to https://shack.us/lost.gif"
        assert row("relocated")[:4] == ("live", "not archived: can't be checked", png(b"sig"),
                                        "https://cdn.art.com/f/ae36/dzniqp-b48f.png?token=x")
        assert row("gone 1")[:2] == row("gone 2")[:2] == ("missing", "original host: a placeholder; not archived")
        assert row("unarchived")[:3] == ("live", "not archived: can't be checked", LATER)
        # a placeholder on the live host and archived: the earlier capture, where there is one
        assert row("stand-in 1")[:3] == ("archived", "original host: a placeholder", png(b"one"))
        assert row("stand-in 2")[:3] == ("missing", "original host: a placeholder; archived copies are "
                                                     "placeholders", None)
        assert row("cut")[:3] == ("archived", "original host: unreachable: URLError(gaierror(11001, "
                                              "'getaddrinfo failed'))", png(b"cut"))
        assert row("nowhere")[0] == "missing" and row("nowhere")[5] == "unreachable (no such host)"
        assert row("spaced")[0] == "live"
        assert sum("old.host.com" in url for url in network["live"]) == 1  # a host gone is asked once
        assert sum(row[1] == "not archived: can't be checked" for row in rows.values()) == 3

        # nothing left to look for; --retry looks again for the missing ones
        assert recover_images(db, Throttle(0)) == {}
        assert sum(recover_images(db, Throttle(0), retry=True).values()) == 6


def test_site_uses_copies(tmp_path):
    class FakeLinks(Links):
        def image(self, url):
            return "files/images/1.png" if url == "http://a.com/x.png" else None

    r = Renderer(FakeLinks())
    assert r.render("[img]http://a.com/x.png[/img][img]http://a.com/y.png[/img]", "../") == (
        '<img class="bb-img" src="../files/images/1.png" alt="" loading="lazy">'
        '<img class="bb-img" src="http://a.com/y.png" alt="" loading="lazy">')

    with open_database(":memory:") as db:
        db.create_schema()
        db.upsert_many("users", [{"id": 1, "name": "wedgie", "signature": None,
                                  "signature_fixed": "[img]HTTP://Img148.imageshack.us/alumni.png[/img]"}])
        collect_images(db, {"signatures": images_module.signature_texts(db)})
        db.update_many("embedded_images", [{"id": 1, "status": "archived", "data": png(b"alumni")}])
        build_site(db, tmp_path, "Board")
    assert (tmp_path / "files/images/1.png").read_bytes() == png(b"alumni")
    assert 'src="../files/images/1.png"' in (tmp_path / "u/1.html").read_text(encoding="utf-8")
