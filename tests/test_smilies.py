import json

import pytest

from tapascrape.content import smilies as smilies_module
from tapascrape.content.smilies import (MISSING_PREFIX, collect_smilies, describe, normalize_url,
                                        recover_smilies)
from tapascrape.db import open_database
from tapascrape.net import wayback
from tapascrape.net.throttle import Throttle
from tapascrape.net.wayback import ArchivedFile, fetch_archived_image, image_type

YAY = "http://static.yuku.com/domain/bypass/images/yay.gif"
HM = "http://2.forumer.com/html/emoticons/metanet/hm.gif"
GIF = b"GIF89a\x01\x00\x01\x00"


def test_urls():
    assert normalize_url("Http://2.FORUMER.com:80/html/emoticons/tongue.gif") == \
        "http://2.forumer.com/html/emoticons/tongue.gif"
    assert describe(YAY) == ("yay", "yuku")
    assert describe("http://2.forumer.com/html/emoticons/metanet/;(.gif") == (";(", "forumer")
    assert describe("http://www.jcxp.net/forums/style_emoticons/default/happyface.gif") == \
        ("happyface", "other")


@pytest.fixture
def db():
    with open_database(":memory:") as db:
        db.create_schema()
        db.upsert_many("posts", [
            {"id": 1, "topic_id": 1, "index": 1, "content": "",
             "source": f"[img]{YAY}[/img] [img]'{HM}'[/img] [img]{YAY}[/img]"},
            {"id": 2, "topic_id": 1, "index": 2, "content": "",
             "source": "[img]HTTP://2.Forumer.com:80/html/emoticons/metanet/hm.gif[/img] "
                       "[img]http://example.com/photo.png[/img]"},
        ])
        yield db


def test_collect_keeps_ids_stable(db):
    assert collect_smilies(db) == 2
    assert db.query("SELECT id, url, name, host, uses FROM smilies ORDER BY id") == [
        (1, HM, "hm", "forumer", 2), (2, YAY, "yay", "yuku", 2)]
    db.upsert_many("posts", [{"id": 3, "topic_id": 1, "index": 3, "content": "",
                              "source": "[img]http://2.forumer.com/html/emoticons/huh.gif[/img]"}])
    assert collect_smilies(db) == 1
    assert db.query("SELECT id, name, uses FROM smilies ORDER BY id") == [
        (1, "hm", 2), (2, "yay", 2), (3, "huh", 1)]


def test_recover(db, monkeypatch):
    collect_smilies(db)
    found = {YAY: ArchivedFile(GIF, "image/gif", "https://web.archive.org/web/2013id_/" + YAY)}
    monkeypatch.setattr(smilies_module, "fetch_archived_image", lambda url, throttle: found.get(url))
    assert recover_smilies(db, Throttle(0)) == 1
    assert db.query("SELECT name, content_type, data FROM smilies WHERE data IS NOT NULL") == [
        ("yay", "image/gif", GIF)]
    assert db.states(MISSING_PREFIX) == {"1": HM}
    assert recover_smilies(db, Throttle(0)) == 0  # missing ones aren't looked up again...
    found[HM] = ArchivedFile(GIF, "image/gif", "x")
    assert recover_smilies(db, Throttle(0), retry=True) == 1  # ...unless asked to


def test_wayback_skips_captures_that_are_not_images(monkeypatch):
    cdx = [["timestamp", "original"], ["2005", "http://2.forumer.com:80/html/emoticons/huh.gif"],
           ["2006", "http://2.forumer.com:80/html/emoticons/huh.gif"]]
    responses = {
        "2005": b"<html>Not found</html>",
        "2006": GIF,
    }

    def fake_fetch(url, throttle, timeout=30.0, max_retries=3):
        if url.startswith(wayback.CDX_URL):
            assert "filter=mimetype%3Aimage%2F.%2A" in url
            return json.dumps(cdx).encode()
        return responses[url.split("/web/")[1].split("id_")[0]]

    monkeypatch.setattr(wayback, "fetch_bytes", fake_fetch)
    found = fetch_archived_image("http://2.forumer.com/html/emoticons/huh.gif", Throttle(0))
    assert found.data == GIF and found.archived_url.startswith("https://web.archive.org/web/2006id_/")
    assert image_type(b"\x89PNG\r\n\x1a\n...") == "image/png" and image_type(b"<html>") is None


TAPATALK_URL = "https://www.tapatalk.com/groups/metanetfr/forum_data/forums.me/meta/metanetfr/smilies/4.gif"
KERON_SIGNATURE = ('loLol<br/><img class="postimage lazyloaded" '
                   'data-src="forum_data/forums.me/meta/metanetfr/smilies/4.gif" '
                   'src="forum_data/forums.me/meta/metanetfr/smilies/4.gif"/>')


def test_tapatalk_urls():
    assert normalize_url("forum_data/forums.me/meta/metanetfr/smilies/4.gif") == TAPATALK_URL
    assert normalize_url(TAPATALK_URL) == TAPATALK_URL
    assert describe(TAPATALK_URL) == ("4", "tapatalk")
    assert smilies_module.tapatalk_board(TAPATALK_URL) == "metanetfr"


def test_signature_smileys_are_collected_and_fetched_live(db, monkeypatch):
    db.upsert_many("users", [{"id": 9370387, "name": "Keron Cyst", "signature": KERON_SIGNATURE},
                             {"id": 2, "name": "Other", "signature": f'<img src="{HM}"/>'}])
    collect_smilies(db)
    assert db.query("SELECT url, host, uses FROM smilies ORDER BY id") == [
        (HM, "forumer", 3), (YAY, "yuku", 2), (TAPATALK_URL, "tapatalk", 1)]
    wayback_calls = []
    monkeypatch.setattr(smilies_module, "fetch_archived_image",
                        lambda url, throttle: wayback_calls.append(url))
    live = {TAPATALK_URL: ArchivedFile(GIF, "image/gif", TAPATALK_URL)}
    recover_smilies(db, Throttle(0), live=live.get)
    assert db.query("SELECT data, recovered_from FROM smilies WHERE host = 'tapatalk'") == [
        (GIF, TAPATALK_URL)]
    assert wayback_calls == [HM, YAY]  # Tapatalk-hosted ones only go there when gone
