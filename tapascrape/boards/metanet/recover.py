"""Recover the old board's files from the Wayback Machine, using the dump's id mappings.

Attachments: forumer served each post's attachment as index.php?act=Attach&
type=post&id=<the post's own id>, often with a session id in front (s=...),
which the generic `attachments` command can't list. Each archived download
fills the act=Attach row of that id and the upload row of the post it belongs
to: the linked post's, or else the only post with a still-missing upload in
the time window the id falls in (old ids grow with time), when the archived
file name says it's the same kind of file.

Avatars: forumer kept uploaded avatars as uploads/metanet/av-<member id>.<ext>;
others were links to image hosts. The dump says which one each member had last.
"""

import logging
from collections import Counter, defaultdict
from email.message import Message
from pathlib import PurePosixPath

from tapascrape.boards.metanet.link import TimeScale, post_upload
from tapascrape.boards.metanet.schema import TABLES
from tapascrape.content.attachments import (MISSING_PREFIX, canonical, filename_from, header,
                                            valid_download)
from tapascrape.db.base import Database
from tapascrape.net.throttle import RetryableError, Throttle
from tapascrape.net.wayback import (Capture, fetch_archived_image, fetch_capture, image_type,
                                    prefix_captures)

log = logging.getLogger(__name__)

ATTACH_STATE = "forumer-attach:"   # crawl_state: act=Attach id -> outcome
AVATAR_STATE = "forumer-avatar:"   # crawl_state: old member id -> outcome
BOARD_PAGES = "http://metanet.2.forumer.com/index.php"
AVATAR_UPLOADS = "http://2.forumer.com/uploads/metanet/av-"


def extension(name: str | None) -> str:
    return PurePosixPath(name or "").suffix.lower()


# -- attachments ---------------------------------------------------------------------------

def attach_captures(throttle: Throttle) -> dict[int, list[Capture]]:
    """act=Attach id -> its archived downloads, session-prefixed URLs included."""
    captures = prefix_captures(BOARD_PAGES, throttle, original=".*[Aa]ct=[Aa]ttach.*")
    by_id: dict[int, list[Capture]] = defaultdict(list)
    for capture in captures:
        found = canonical(capture.original)
        if found is not None and found.old_attach_id is not None and "type=post" in found.url:
            by_id[found.old_attach_id].append(capture)
    log.info("attachments: %d archived act=Attach downloads of %d attachments",
             len(captures), len(by_id))
    return by_id


def recover_attachments(db: Database, throttle: Throttle, retry: bool = False,
                        max_captures: int = 3) -> Counter:
    db.create_schema(TABLES)
    done = {} if retry else db.states(ATTACH_STATE)
    by_id = attach_captures(throttle)
    by_url = dict(db.query("SELECT url, id FROM attachments"))
    owners = dict(db.query("SELECT f.id, p.source FROM forumer_posts f JOIN posts p ON p.id = f.post_id "
                           "WHERE f.match <> 'interpolated'"))
    scale = TimeScale(db)
    # Uploads still missing, with the time of the post they came with (posts linked to an
    # old id are that id's, so they can't be another's).
    linked = {post_id for post_id, in db.query(
        "SELECT post_id FROM forumer_posts WHERE post_id IS NOT NULL")}
    missing_uploads = [
        (attachment_id, name, timestamp)
        for attachment_id, name, timestamp, post_id, source in db.query(
            "SELECT a.id, a.name, p.timestamp, p.id, p.source FROM attachments a "
            "JOIN posts p ON p.id = a.first_post_id WHERE a.kind = 'upload' AND a.data IS NULL")
        if post_id not in linked and post_upload(source, by_url) == attachment_id]
    report: Counter = Counter()
    for attach_id in sorted(by_id):
        if str(attach_id) in done:
            report["already looked at"] += 1
            continue
        targets, upload, guessed = _targets(db, attach_id, owners, by_url, scale, missing_uploads)
        if not targets:
            db.set_state(f"{ATTACH_STATE}{attach_id}", "nothing missing")
            db.commit()
            report["nothing missing"] += 1
            continue
        upload_name = (db.query("SELECT name FROM attachments WHERE id = ?", (upload,))[0][0]
                       if upload else None)
        try:
            # A guessed upload doesn't vouch for the download: check it on its own name.
            found = _download(by_id[attach_id], throttle, None if guessed else upload_name, max_captures)
        except RetryableError as e:
            log.warning("act=Attach id %d: %s; will retry on the next run", attach_id, e)
            report["failed, retry later"] += 1
            continue
        with db.transaction():
            if isinstance(found, str):
                db.set_state(f"{ATTACH_STATE}{attach_id}", found)
                report[found] += 1
                continue
            data, headers, archived = found
            filename = filename_from(headers)
            if guessed and (filename is None or extension(filename) != extension(upload_name)):
                targets.discard(upload)  # the archived file name doesn't confirm the guess
                upload, guessed = None, False
                if not targets:
                    db.set_state(f"{ATTACH_STATE}{attach_id}", "time-window guess not confirmed")
                    report["time-window guess not confirmed"] += 1
                    continue
            for target in targets:
                row = {"id": target, "data": data, "size": len(data), "recovered_from": archived,
                       "content_type": image_type(data) or header(headers, "content-type")}
                if target != upload and filename:
                    row["name"] = filename  # act=Attach rows: the original name
                db.update_many("attachments", [row])
                db.delete_state(f"{MISSING_PREFIX}{target}")
            db.upsert_many("forumer_attachments", [{
                "old_post_id": attach_id, "ref": str(attach_id), "kind": "file", "name": filename,
                "attachment_id": upload}])
            db.set_state(f"{ATTACH_STATE}{attach_id}", "recovered" + (" (time window)" if guessed else ""))
        report["recovered" + (" by time window" if guessed else "")] += 1
        log.info("act=Attach id %d: recovered %s (%d bytes) -> attachments %s", attach_id,
                 filename or "?", len(data), sorted(targets))
    return report


def _targets(db: Database, attach_id: int, owners: dict, by_url: dict, scale: TimeScale,
             missing_uploads: list) -> tuple[set[int], int | None, bool]:
    """(attachments rows still missing the file, its upload row, whether that was a guess)."""
    targets = {row_id for row_id, in db.query(
        "SELECT id FROM attachments WHERE old_attach_id = ? AND data IS NULL", (attach_id,))}
    upload, guessed = post_upload(owners.get(attach_id), by_url), False
    if upload is None and attach_id not in owners and (window := scale.window(attach_id)):
        low, high = window
        inside = [row_id for row_id, _, timestamp in missing_uploads if low <= timestamp <= high]
        if len(inside) == 1:
            upload, guessed = inside[0], True
    if upload is not None and db.query("SELECT 1 FROM attachments WHERE id = ? AND data IS NULL", (upload,)):
        targets.add(upload)
    return targets, upload, guessed


def _download(captures: list[Capture], throttle: Throttle, name: str | None,
              max_captures: int) -> tuple[bytes, Message, str] | str:
    """The first archived download that is the file, or why there's none."""
    for capture in sorted(captures, key=lambda c: c.timestamp)[:max_captures]:
        found = fetch_capture(capture, throttle)
        if found is not None and valid_download(found[0], found[1], name):
            return found
    return "archived copies aren't the file"


# -- avatars -----------------------------------------------------------------------------------

def avatar_captures(throttle: Throttle) -> dict[int, list[Capture]]:
    """Old member id -> archived copies of their uploaded avatar (any extension)."""
    by_member: dict[int, list[Capture]] = defaultdict(list)
    for capture in prefix_captures(AVATAR_UPLOADS, throttle):
        name = PurePosixPath(capture.original.split("?", 1)[0]).stem  # av-123
        if name[3:].isdigit() and capture.mimetype.startswith("image/"):
            by_member[int(name[3:])].append(capture)
    return by_member


def recover_avatars(db: Database, throttle: Throttle, retry: bool = False) -> Counter:
    """Fill forumer_avatars: each member's last avatar on the old board, where archived."""
    db.create_schema(TABLES)
    done = {} if retry else db.states(AVATAR_STATE)
    stored = {old_id for old_id, in db.query("SELECT old_id FROM forumer_avatars")}
    uploads = avatar_captures(throttle)
    log.info("avatars: archived uploads for %d members", len(uploads))
    members = dict(db.query("SELECT old_id, avatar_url FROM forumer_members"))
    todo = sorted(old_id for old_id in members.keys() | uploads.keys()
                  if (members.get(old_id) or uploads.get(old_id))
                  and old_id not in stored and str(old_id) not in done)
    report: Counter = Counter({"already done": len(stored | {int(k) for k in done})})
    for n, old_id in enumerate(todo, 1):
        url = members.get(old_id)
        try:
            found = _avatar(url, uploads.get(old_id, []), throttle)
        except RetryableError as e:
            log.warning("avatar of member %d: %s; will retry on the next run", old_id, e)
            report["failed, retry later"] += 1
            continue
        with db.transaction():
            if found is None:
                db.set_state(f"{AVATAR_STATE}{old_id}", "not archived")
                report["not archived"] += 1
                continue
            original, data, archived = found
            db.upsert_many("forumer_avatars", [{"old_id": old_id, "url": original, "data": data,
                                                "content_type": image_type(data),
                                                "recovered_from": archived}])
            db.set_state(f"{AVATAR_STATE}{old_id}", "recovered")
        report["recovered"] += 1
        if n % 25 == 0:
            log.info("avatars: %d/%d looked up, %d recovered", n, len(todo), report["recovered"])
    return report


def _avatar(url: str | None, uploads: list[Capture], throttle: Throttle) -> tuple[str, bytes, str] | None:
    """(original URL, image, archived URL) of a member's avatar, preferring the one the dump saw
    last and, among copies, the latest."""
    same = [c for c in uploads if url and _same_file(c.original, url)]
    others = [c for c in uploads if c not in same]
    for capture in sorted(same, key=lambda c: c.timestamp, reverse=True) + \
            sorted(others, key=lambda c: c.timestamp, reverse=True):
        found = fetch_capture(capture, throttle)
        if found is not None and image_type(found[0]):
            return capture.original, found[0], found[2]
    if url and not same and "/uploads/metanet/av-" not in url:  # linked from an image host
        if (archived := fetch_archived_image(url, throttle)) is not None:
            return url, archived.data, archived.archived_url
    return None


def _same_file(a: str, b: str) -> bool:
    def key(url: str) -> str:
        return url.split("://", 1)[-1].replace(":80/", "/").split("?", 1)[0].lower()
    return key(a) == key(b)
