"""Files uploaded to the old forumer (Invision) board -> the `attachments` table.

Posts reference them in three forms:

- the attachment block every migrated post with attachments ends with,
  "--------------------[url=/attach/ma/post-10-1081445810.txt]Click here to
  view the attachment[/url]": a relative link to the file forumer kept as
  uploads/metanet/post-<member>-<unix time>.<ext>;
- direct links and [img] embeds of those uploads (and a few other forumer
  boards'): http://2.forumer.com/uploads/metanet/...;
- forumer's attachment downloads by id: index.php?act=Attach&type=post&id=N.

Each distinct file gets a row with a stable id of ours, which fixed sources use
(see content.fix). forumer's domains are parked now (any URL answers with an
HTML page), so files are recovered from the Wayback Machine where archived;
downloads are checked to really be the file, not some HTML page.
"""

import logging
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import Message
from pathlib import PurePosixPath
from urllib.parse import parse_qsl, unquote, urlsplit

from tapascrape.content.scan import iter_signature_sources, iter_sources
from tapascrape.db.base import Database
from tapascrape.net.http import fetch
from tapascrape.net.throttle import RetryableError, Throttle
from tapascrape.net.wayback import Capture, fetch_capture, image_type, prefix_captures

log = logging.getLogger(__name__)

MISSING_PREFIX = "attachment-missing:"  # crawl_state: not found anywhere (value: why)
UPLOADS = "http://2.forumer.com/uploads/metanet/"  # where /attach/ma/ files lived

_URL_CHARS = r"[^\s\[\]'\"<>]"
# Any reference to an attachment, as a bare URL (also found inside [url]/[img] tags).
ATTACHMENT_REF = re.compile(
    rf"/attach/ma/{_URL_CHARS}+"
    rf"|(?:https?://)?[\w.-]*forumer\.com(?::\d+)?/+uploads/{_URL_CHARS}+"
    rf"|https?://[\w.-]*forumer\.com(?::\d+)?/index\.php\?{_URL_CHARS}*act=attach{_URL_CHARS}*",
    re.IGNORECASE)
TRAILING_PUNCTUATION = ".,;:!?)"
UPLOAD_NAME = re.compile(r"post-(\d+)-(\d{9,10})\.\w+", re.IGNORECASE)
IMAGE_EXTENSIONS = {"png", "gif", "jpg", "jpeg", "bmp"}
MAX_LIVE_MISSES = 20  # files in a row a live host doesn't have before it's given up on


def may_reference(text: str) -> bool:
    """Cheap test before running ATTACHMENT_REF on a text."""
    low = text.lower()
    return "/attach/" in low or "/uploads/" in low or "act=attach" in low


@dataclass(frozen=True)
class Attachment:
    kind: str  # "upload" or "attach-id"
    url: str   # canonical
    name: str | None = None
    old_member_id: int | None = None
    uploaded_at: datetime | None = None
    old_attach_id: int | None = None


def canonical(ref: str) -> Attachment | None:
    """The attachment a reference (as found by ATTACHMENT_REF) points to."""
    ref = ref.rstrip(TRAILING_PUNCTUATION)
    if ref.lower().startswith("/attach/ma/"):
        return upload(UPLOADS + ref[len("/attach/ma/"):])
    if "://" not in ref:
        ref = "http://" + ref
    parts = urlsplit(ref.replace("&amp;", "&"))
    host = parts.netloc.lower().removesuffix(":80")
    if "/uploads/" in parts.path.lower():
        return upload(f"http://{host}{re.sub(r'/+', '/', parts.path)}")
    query = {k.lower(): v for k, v in parse_qsl(parts.query, keep_blank_values=True)}
    if query.get("act", "").lower() == "attach" and query.get("id", "").isdigit():
        attach_id = int(query["id"])
        kind = query.get("type") or "post"
        return Attachment("attach-id", f"http://{host}/index.php?act=Attach&type={kind}&id={attach_id}",
                          old_attach_id=attach_id)
    return None


def upload(url: str) -> Attachment | None:
    name = unquote(PurePosixPath(urlsplit(url).path).name)
    if not name:
        return None
    member = uploaded = None
    if match := UPLOAD_NAME.fullmatch(name):
        member = int(match.group(1))
        uploaded = datetime.fromtimestamp(int(match.group(2)), tz=timezone.utc).replace(tzinfo=None)
    return Attachment("upload", url, name, member, uploaded)


def attachment_ids(db: Database) -> dict[str, int]:
    """Canonical URL -> attachment id."""
    return {url: attachment_id for attachment_id, url in db.query("SELECT id, url FROM attachments")}


def collect_attachments(db: Database) -> int:
    """Add a row for every attachment referenced in posts and signatures; refresh counts.

    Existing rows keep their ids; new ones get the next ids, in order of first
    use. Returns the number of new attachments.
    """
    found: dict[str, Attachment] = {}
    uses: Counter = Counter()
    first_post: dict[str, int] = {}
    for texts, is_post in ((iter_sources(db), True), (iter_signature_sources(db), False)):
        for row_id, text in texts:
            if not may_reference(text):
                continue
            for match in ATTACHMENT_REF.finditer(text):
                attachment = canonical(match.group(0))
                if attachment is None:
                    continue
                found.setdefault(attachment.url, attachment)
                uses[attachment.url] += 1
                if is_post:
                    first_post[attachment.url] = min(first_post.get(attachment.url, row_id), row_id)
    known = attachment_ids(db)
    next_id = max(known.values(), default=0) + 1
    order = sorted(found, key=lambda url: (first_post.get(url) is None, first_post.get(url, 0), url))
    updates, inserts = [], []
    for url in order:
        counts = {"uses": uses[url], "first_post_id": first_post.get(url)}
        if url in known:
            updates.append({"id": known[url], **counts})
            continue
        a = found[url]
        inserts.append({"id": next_id, "kind": a.kind, "url": a.url, "name": a.name,
                        "old_member_id": a.old_member_id, "uploaded_at": a.uploaded_at,
                        "old_attach_id": a.old_attach_id, **counts})
        next_id += 1
    with db.transaction():
        db.update_many("attachments", updates)
        db.upsert_many("attachments", inserts)
    log.info("attachments: %d distinct (%d new), %d references",
             len(found), len(inserts), sum(uses.values()))
    return len(inserts)


# -- recovery ------------------------------------------------------------------

def looks_like_file(data: bytes, name: str | None) -> bool:
    """Whether `data` can be the file itself (not a parked domain's or error HTML page)."""
    if not data:
        return False
    ext = name.rsplit(".", 1)[-1].lower() if name and "." in name else ""
    if ext in IMAGE_EXTENSIONS:
        return image_type(data) is not None
    head = data[:512].lstrip().lower()
    if ext not in ("html", "htm") and (head.startswith(b"<!doctype html") or head.startswith(b"<html")):
        return False
    return True


def header(headers: Message, name: str) -> str | None:
    """A header of the original response (Wayback prefixes them with x-archive-orig-)."""
    return headers.get(f"x-archive-orig-{name}") or headers.get(name)


def filename_from(headers: Message) -> str | None:
    disposition = header(headers, "content-disposition") or ""
    match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)\"?", disposition, re.IGNORECASE)
    return unquote(match.group(1)).strip() if match else None


def valid_download(data: bytes, headers: Message, name: str | None) -> bool:
    """Whether a response is the attachment itself.

    Without a known name (act=Attach downloads), a real download says which file
    it is (Content-Disposition); an HTML answer without one is an error page
    (forumer's archived "Can't connect to local MySQL server..." and the like).
    """
    name = name or filename_from(headers)
    if name is None and (header(headers, "content-type") or "").lower().startswith("text/html"):
        return False
    return looks_like_file(data, name)


def capture_key(capture: Capture) -> str | None:
    """The canonical URL an archived capture is a copy of."""
    attachment = canonical(capture.original)
    return attachment.url if attachment is not None else None


def wayback_index(urls: list[tuple[str, str]], throttle: Throttle) -> dict[str, list[Capture]]:
    """Canonical URL -> archived captures, listing whole directories at once."""
    prefixes = set()
    for kind, url in urls:
        if kind == "upload":
            prefixes.add(url.rsplit("/", 1)[0] + "/")
        else:
            prefixes.add(url.split("?", 1)[0] + "?act=Attach")
    index: dict[str, list[Capture]] = {}
    for prefix in sorted(prefixes):
        captures = prefix_captures(prefix, throttle)
        for capture in captures:
            if (key := capture_key(capture)) is not None:
                index.setdefault(key, []).append(capture)
        log.info("attachments: %d captures archived under %s", len(captures), prefix)
    return index


def recover_attachments(db: Database, throttle: Throttle, live_throttle: Throttle | None = None,
                        retry: bool = False, max_captures: int = 3) -> int:
    """Download the files of attachments without data: from their original host
    while it still serves them, else from the Wayback Machine."""
    if retry:
        with db.transaction():
            for key in db.states(MISSING_PREFIX):
                db.delete_state(f"{MISSING_PREFIX}{key}")
    missing = {int(k) for k in db.states(MISSING_PREFIX)}
    todo = [row for row in db.query("SELECT id, kind, url, name FROM attachments "
                                    "WHERE data IS NULL ORDER BY id") if row[0] not in missing]
    if not todo:
        log.info("attachments: nothing to recover")
        return 0
    log.info("attachments: %d without a file", len(todo))
    index = wayback_index([(kind, url) for _, kind, url, _ in todo], throttle)
    live_throttle = live_throttle or Throttle(1.0)
    dead_hosts: set[str] = set()
    misses: Counter = Counter()  # consecutive files a host didn't have
    recovered = 0
    for attachment_id, kind, url, name in todo:
        try:
            found = _recover(url, name, index.get(url, []), throttle, live_throttle, dead_hosts,
                             misses, max_captures)
        except RetryableError as e:
            log.warning("attachment %d (%s): %s; will retry on the next run", attachment_id, url, e)
            continue
        with db.transaction():
            if isinstance(found, str):
                db.set_state(f"{MISSING_PREFIX}{attachment_id}", found)
                continue
            data, headers, source = found
            row = {"id": attachment_id, "data": data, "size": len(data), "recovered_from": source,
                   "content_type": image_type(data) or header(headers, "content-type")}
            if name is None and (filename := filename_from(headers)):
                row["name"] = filename
            db.update_many("attachments", [row])
        log.info("attachment %d: recovered %s (%d bytes)", attachment_id, url, len(data))
        recovered += 1
    log.info("attachments: %d recovered, %d not found anywhere",
             recovered, len(db.states(MISSING_PREFIX)))
    return recovered


def _recover(url: str, name: str | None, captures: list[Capture], throttle: Throttle,
             live_throttle: Throttle, dead_hosts: set[str], misses: Counter,
             max_captures: int) -> tuple[bytes, Message, str] | str:
    """(data, headers, where from), or why there's nothing."""
    host = urlsplit(url).netloc
    if host not in dead_hosts:
        try:
            found = fetch(url, live_throttle, timeout=30, max_retries=1)
        except RetryableError:
            found, dead = None, True  # unreachable
        else:
            # Parked domains answer anything with the same page (a 404 is just one file).
            dead = found is not None and not valid_download(found[0], found[1], name)
        if found is not None and not dead:
            misses[host] = 0
            return found[0], found[1], url
        misses[host] += 1
        if dead or misses[host] >= MAX_LIVE_MISSES:
            log.info("%s doesn't serve the files anymore; using the Wayback Machine only", host)
            dead_hosts.add(host)
    if not captures:
        return "not archived"
    for capture in sorted(captures, key=lambda c: c.timestamp)[:max_captures]:
        found = fetch_capture(capture, throttle)
        if found is not None and valid_download(found[0], found[1], name):
            return found
    return "archived copies aren't the file"
