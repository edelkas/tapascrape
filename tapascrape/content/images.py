"""Images embedded with [img] from other hosts -> the `embedded_images` table, with copies.

Image hosts prune old files (ImageShack, TinyPic and Photobucket's free
albums), and whole sites go. Each distinct URL embedded where SCOPES look
(signatures, for now; boards can add texts of their own) gets a row with a
stable id of ours, and the site shows the copy kept instead of the URL.
Smileys and forumer uploads aren't included: `smilies` and `attachments` have
them.

A copy is looked for in three passes, each resumable (a row keeps how far it
got):

1. The original host. Its answer counts only if it's a whole image (GIF, PNG,
   JPEG or BMP, not an HTML page or one cut short). Hosts redirect to where
   files moved (deviantART), but also to an "image not found" picture.
2. The Wayback Machine's captures of the URL that were images, one per change
   of content, with their digests (SHA-1 of the file).
3. The decision:
   - a live copy that is byte for byte the oldest capture is kept;
   - else the oldest capture that is a whole image: hosts alter, overwrite and
     replace files (Photobucket now stamps a watermark on old images; others
     serve a "removed" picture), and the Wayback Machine archives those too;
   - else the live copy, unless it's a placeholder.

A placeholder is the same picture served for several different file names,
or one a redirect led to that several URLs redirect to or that's named like
one ("removed.png"). Wayback captures can be placeholders too (a host's
stand-in, archived after the image went): the same picture at several URLs,
which replaced an earlier one at some of them. Neither is ever kept.
"""

import html
import json
import logging
import re
import threading
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from datetime import datetime, timezone
from pathlib import PurePosixPath
from urllib.parse import quote, unquote, urlsplit

from tapascrape.content.attachments import ATTACHMENT_REF
from tapascrape.content.smilies import SMILEY_IMG
from tapascrape.crawl.progress import Progress
from tapascrape.db.base import Database
from tapascrape.net.http import fetch_final
from tapascrape.net.throttle import RetryableError, Throttle
from tapascrape.net.wayback import (URL_SAFE, Capture, fetch_capture, image_captures, image_type,
                                    payload_digest)
from tapascrape.parse.bbcode import html_to_bbcode

log = logging.getLogger(__name__)

IMG = re.compile(r"\[img(?:=[^\]]*)?\](.*?)\[/img\]", re.IGNORECASE | re.DOTALL)
URL = re.compile(r"(https?)://([^/?#\s]+)(.*)", re.IGNORECASE | re.DOTALL)
PLACEHOLDER_NAMES = 3      # file names a picture is served for to be a host's stand-in
MAX_CAPTURES = 3           # archived versions tried per image
LIVE_RATE = 2.0            # requests per second to each image host
LIVE_WORKERS = 8           # image hosts asked at once
ARCHIVE_WORKERS = 4        # Wayback Machine requests at once (it answers slowly; --rate still holds)
LIVE_FAILURES = 3          # unreachable answers in a row before a host is given up on
ARCHIVE_FAILURES = 3       # failed Wayback lookups in a row before the pass stops
MOVED = "moved to "        # `live` of an image a redirect led to another file for
# The name of a host's stand-in for a file it doesn't have.
STAND_IN_NAME = re.compile(r"(?:^|[^a-z])(?:removed|deleted|missing|not[_-]?found|(?:not|un)available|"
                           r"no[_-]?(?:image|img|pic|photo)|placeholder|error|404|lost|blank|spacer|default)"
                           r"(?:[^a-z]|$)", re.IGNORECASE)
UNREACHABLE_HOST = ("getaddrinfo", "name or service not known", "nodename nor servname",
                    "no address associated")


def image_url(raw: str) -> str | None:
    """One spelling per embedded image: absolute http(s), lowercase scheme and host,
    no default port or fragment. None if it isn't one."""
    url = html.unescape(raw).strip().strip("'\"").strip().split("#", 1)[0]
    if url.lower().startswith("www."):
        url = "http://" + url
    match = URL.fullmatch(url)
    if match is None:
        return None
    host = match.group(2).lower().removesuffix(":80")
    return f"{match.group(1).lower()}://{host}{match.group(3)}"


def handled_elsewhere(url: str) -> bool:
    """Smileys and forumer uploads, which have tables of their own."""
    return bool(SMILEY_IMG.fullmatch(f"[img]{url}[/img]") or ATTACHMENT_REF.match(url))


def host_of(url: str) -> str:
    return urlsplit(url).netloc.lower()


def site_of(host: str) -> str:
    """i37.photobucket.com -> photobucket.com: the hosts one service answers from."""
    return ".".join(host.split(":", 1)[0].split(".")[-2:])


def file_name(url: str) -> str:
    """The file name a URL asks for (with its query), to tell files apart."""
    parts = urlsplit(url)
    name = unquote(PurePosixPath(parts.path).name).lower()
    return f"{name}?{parts.query}" if parts.query else name


def request_url(url: str) -> str:
    """`url` as it can be requested (spaces and other characters percent-encoded)."""
    return quote(url, safe=URL_SAFE)


# -- where images are embedded -------------------------------------------------------------------

def signature_texts(db: Database) -> Iterator[str]:
    """Users' signatures, as the site shows them (fixed BBCode, else rebuilt from the HTML)."""
    for fixed, source, signature in db.query(
            "SELECT signature_fixed, signature_source, signature FROM users WHERE id <> 0"):
        text = fixed or source or (html_to_bbcode(signature) if signature else None)
        if text:
            yield text


# scope -> its texts; the scopes the `images` command reads.
SCOPES: dict[str, Callable[[Database], Iterable[str]]] = {"signatures": signature_texts}


def embedded_urls(text: str) -> Iterator[str]:
    """The images a BBCode text embeds, but smileys and forumer uploads."""
    if "[img" not in text.lower():
        return
    for match in IMG.finditer(text):
        url = image_url(match.group(1))
        if url is not None and not handled_elsewhere(url):
            yield url


def collect_images(db: Database, texts: dict[str, Iterable[str]]) -> int:
    """Add a row for every image embedded in `texts` (scope -> BBCode texts); refresh
    where and how often each is used. Existing rows keep their ids; new ones get the
    next ids, most used first. Returns the number of new images."""
    uses: Counter = Counter()
    scopes: dict[str, set[str]] = defaultdict(set)
    for scope, items in texts.items():
        for text in items:
            for url in embedded_urls(text):
                uses[url] += 1
                scopes[url].add(scope)
    known = dict(db.query("SELECT url, id FROM embedded_images"))
    next_id = max(known.values(), default=0) + 1
    updates = [{"id": image_id, "uses": uses[url], "used_in": ",".join(sorted(scopes[url])) or None}
               for url, image_id in known.items()]
    inserts = []
    for url in sorted(set(uses) - set(known), key=lambda url: (-uses[url], url)):
        inserts.append({"id": next_id, "url": url, "host": host_of(url), "uses": uses[url],
                        "used_in": ",".join(sorted(scopes[url]))})
        next_id += 1
    with db.transaction():
        db.update_many("embedded_images", updates)
        db.upsert_many("embedded_images", inserts)
    log.info("images: %d distinct embedded (%d new), %d uses", len(uses), len(inserts),
             sum(uses.values()))
    return len(inserts)


# -- checking a copy -----------------------------------------------------------------------------

def whole_image(data: bytes) -> str | None:
    """The type of an image that's all there (the end of the file too), else None."""
    mime = image_type(data)
    tail = data[-1024:]
    if mime == "image/png":
        whole = b"IEND" in tail
    elif mime == "image/jpeg":
        whole = b"\xff\xd9" in tail
    elif mime == "image/gif":
        whole = data.rstrip(b"\x00\r\n ").endswith(b";")
    elif mime == "image/bmp":
        whole = len(data) >= int.from_bytes(data[2:6], "little")
    else:
        return None
    return mime if whole else None


def same_file(url: str, final: str) -> bool:
    """Whether a redirect from `url` to `final` still asks for the same file (another
    scheme or host doesn't count)."""
    return file_name(url) == file_name(final)


def moved_placeholders(moved: Iterable[tuple[str, str]]) -> set[str]:
    """Digests of the images redirects led to (from (final URL, digest)) that are a host's
    stand-in: the same file for several URLs, or one named like one."""
    moved = [(final.split("?", 1)[0], digest) for final, digest in moved]
    targets = Counter(final for final, _ in moved)
    return {digest for final, digest in moved
            if targets[final] >= 2 or STAND_IN_NAME.search(file_name(final))}


def placeholders(served: Iterable[tuple[str, str]]) -> set[str]:
    """Digests served for PLACEHOLDER_NAMES file names or more, from (url, digest)."""
    names: dict[str, set[str]] = defaultdict(set)
    for url, digest in served:
        names[digest].add(file_name(url))
    return {digest for digest, found in names.items() if len(found) >= PLACEHOLDER_NAMES}


def archived_placeholders(captures: dict[str, list[list[str]]]) -> set[str]:
    """Digests of captures that are placeholders: at several URLs, and at some of them
    after an earlier, different file (a stand-in archived once the image was gone)."""
    replaced = set()
    for rows in captures.values():
        replaced |= {digest for _, _, digest in rows[1:] if digest != rows[0][2]}
    shared = placeholders((url, digest) for url, rows in captures.items() for _, _, digest in rows)
    return shared & replaced


def in_parallel(fn: Callable, items: list, workers: int) -> Iterator[tuple]:
    """(item, fn(item)) for each item as it's done, `workers` at a time. A RetryableError
    is the result, not raised."""
    def run(item):
        try:
            return fn(item)
        except RetryableError as e:
            return e

    pool = ThreadPoolExecutor(workers)
    try:
        futures = {pool.submit(run, item): item for item in items}
        for future in as_completed(futures):
            yield futures[future], future.result()
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


# -- 1. the original hosts -----------------------------------------------------------------------

class LiveHosts:
    """Asks the original hosts for images, a few at a time but each service (all of
    photobucket.com's hosts, say) slowly; gives up on hosts that are gone."""

    def __init__(self, rate: float | None = None):
        self.rate = rate or LIVE_RATE
        self.lock = threading.Lock()
        self.throttles: dict[str, Throttle] = {}
        self.failures: Counter = Counter()  # unreachable answers in a row, per host
        self.gone: dict[str, str] = {}      # host -> why

    def throttle(self, site: str) -> Throttle:
        with self.lock:
            return self.throttles.setdefault(site, Throttle(self.rate))

    def fetch(self, url: str) -> tuple[str, bytes | None, str | None]:
        """(outcome, image, its type): outcome is "ok" or why the answer isn't the image."""
        host = host_of(url)
        if host in self.gone:
            return f"unreachable ({self.gone[host]})", None, None
        try:
            found = fetch_final(request_url(url), self.throttle(site_of(host)), timeout=20,
                                max_retries=0)
        except RetryableError as e:
            reason = str(e)
            with self.lock:
                self.failures[host] += 1
                if any(text in reason.lower() for text in UNREACHABLE_HOST):
                    self.gone[host] = "no such host"
                elif self.failures[host] >= LIVE_FAILURES:
                    self.gone[host] = "not answering"
            return f"unreachable: {reason}", None, None
        with self.lock:
            self.failures[host] = 0
        if found is None:
            return "not found (HTTP 4xx)", None, None
        data, headers, final = found
        moved = not same_file(request_url(url), final)
        if image_type(data) is None:
            if moved:
                return f"redirected to {final[:150]}", None, None
            kind = (headers.get("content-type") or "unknown type").split(";")[0]
            return f"not an image ({kind})", None, None
        mime = whole_image(data)
        if mime is None:
            return "incomplete image", None, None
        return (f"{MOVED}{final[:300]}" if moved else "ok"), data, mime


def check_live(db: Database, rows: list[tuple[int, str]]) -> None:
    """Pass 1: ask the original hosts for the images of `rows` (id, url)."""
    if not rows:
        return
    hosts = LiveHosts()
    progress = Progress("images: original hosts", len(rows))
    served = 0
    batch = []
    for (image_id, _), (outcome, data, mime) in in_parallel(lambda row: hosts.fetch(row[1]), rows,
                                                            LIVE_WORKERS):
        served += data is not None
        batch.append({"id": image_id, "live": outcome[:500],
                      "live_digest": payload_digest(data) if data else None,
                      "content_type": mime, "size": len(data) if data else None, "data": data})
        if len(batch) >= 20:
            with db.transaction():
                db.update_many("embedded_images", batch)
            batch = []
        progress.advance(detail=f"{served:,} still served")
    with db.transaction():
        db.update_many("embedded_images", batch)
    if hosts.gone:
        log.info("images: %d hosts are gone: %s", len(hosts.gone), ", ".join(sorted(hosts.gone)))


# -- 2. the Wayback Machine ----------------------------------------------------------------------

def look_up_captures(db: Database, rows: list[tuple[int, str]], throttle: Throttle) -> bool:
    """Pass 2: list the archived captures of `rows` (id, url). False if the Wayback
    Machine stopped answering (the rest is left for the next run)."""
    if not rows:
        return True
    progress = Progress("images: Wayback Machine lookups", len(rows))
    archived = failures = 0
    with closing(in_parallel(lambda row: image_captures(row[1], throttle), rows, ARCHIVE_WORKERS)) as done:
        for (image_id, url), captures in done:
            if isinstance(captures, RetryableError):
                failures += 1
                log.warning("images: looking up %s failed (%s)", url, captures)
                if failures >= ARCHIVE_FAILURES:
                    log.error("images: the Wayback Machine isn't answering; rerun later to continue")
                    return False
                continue
            failures = 0
            archived += bool(captures)
            with db.transaction():
                db.update_many("embedded_images", [{"id": image_id, "captures": json.dumps(
                    [[c.timestamp, c.original, c.digest] for c in captures])}])
            progress.advance(detail=f"{archived:,} archived")
    return True


# -- 3. the decision -----------------------------------------------------------------------------

def capture_time(timestamp: str) -> datetime | None:
    try:
        return datetime.strptime(timestamp[:14], "%Y%m%d%H%M%S")
    except ValueError:
        return None


def download_capture(rows: list[list[str]], skip: set[str], throttle: Throttle,
                     max_captures: int = MAX_CAPTURES) -> tuple[dict, str] | str:
    """(the row's new values, outcome) of the oldest capture that is a whole image, or
    why there's none. A capture that's an image but not all there is a last resort."""
    tried, partial = set(), None
    for timestamp, original, digest in rows:
        if digest in skip or digest in tried:
            continue
        if len(tried) >= max_captures:
            break
        tried.add(digest)
        found = fetch_capture(Capture(original, timestamp, "", digest), throttle)
        if found is None:
            continue
        data, _, archived = found
        got = payload_digest(data)
        if got in skip or image_type(data) is None:
            continue
        # The Wayback Machine may answer with another capture of the URL than the one asked.
        at = next((ts for ts, _, d in rows if d == got), timestamp)
        values = {"data": data, "size": len(data), "content_type": image_type(data),
                  "recovered_from": archived, "captured_at": capture_time(at)}
        if whole_image(data):
            return values, "whole"
        partial = partial or values
    if partial is not None:
        return partial, "incomplete"
    if not rows:
        return "not archived"
    return "archived copies are placeholders" if not tried else "archived copies aren't images"


def decide(db: Database, ids: list[int], throttle: Throttle) -> Counter:
    """Pass 3: keep a copy of each image in `ids` (all looked up), or record why there's none."""
    report: Counter = Counter()
    if not ids:
        return report
    served = db.query("SELECT url, live, live_digest FROM embedded_images WHERE live_digest IS NOT NULL")
    live_stand_ins = placeholders((url, digest) for url, _, digest in served) | moved_placeholders(
        (live[len(MOVED):], digest) for _, live, digest in served if live.startswith(MOVED))
    all_captures = {url: json.loads(captures) for url, captures in db.query(
        "SELECT url, captures FROM embedded_images WHERE captures IS NOT NULL")}
    archived_stand_ins = archived_placeholders(all_captures)
    stand_ins = live_stand_ins | archived_stand_ins
    wanted = set(ids)
    rows = [row for row in db.query("SELECT id, url, live, live_digest, captures FROM embedded_images "
                                     "WHERE live IS NOT NULL AND captures IS NOT NULL ORDER BY id")
            if row[0] in wanted]
    progress = Progress("images: copies", len(rows))
    kept = 0
    for (image_id, url, live, *_), found in in_parallel(
            lambda row: _decide(row[2], row[3], json.loads(row[4]), stand_ins, throttle), rows,
            ARCHIVE_WORKERS):
        if isinstance(found, RetryableError):
            log.warning("images: downloading %s failed (%s); left for the next run", url, found)
            report["left for the next run"] += 1
            continue
        values, outcome = found
        values.update(id=image_id, checked_at=datetime.now(timezone.utc).replace(tzinfo=None))
        if values["status"] == "live":
            values["recovered_from"] = live[len(MOVED):] if live.startswith(MOVED) else url
        with db.transaction():
            db.update_many("embedded_images", [values])
        report[outcome] += 1
        kept += values["status"] != "missing"
        progress.advance(detail=f"{kept:,} kept")
    return report


def _decide(live: str, live_digest: str | None, rows: list[list[str]], stand_ins: set[str],
            throttle: Throttle) -> tuple[dict, str]:
    """(the row's new values, outcome for the report)."""
    live_ok = live_digest is not None and live_digest not in stand_ins
    if live_digest is not None and not live_ok:
        live = "a placeholder"
    # The oldest archived version is the board's: hosts alter, overwrite and replace files
    # later, and the Wayback Machine archives those too.
    oldest = next((row for row in rows if row[2] not in stand_ins), None)
    if live_ok and oldest is not None and oldest[2] == live_digest:
        return ({"status": "live", "note": "the same as the Wayback Machine's oldest copy",
                 "captured_at": capture_time(oldest[0])}, "kept the original host's copy, as archived")
    found = download_capture(rows, stand_ins, throttle)
    if not isinstance(found, str):
        values, whole = found
        if live_ok:
            note = "the original host's copy has changed since"
        else:
            note = f"original host: {live}"
        if whole == "incomplete":
            note += "; the archived image is incomplete"
        outcome = ("archived copy, the original host's has changed" if live_ok
                   else "archived copy, the original host has a placeholder" if live == "a placeholder"
                   else "archived copy, the original host doesn't have it")
        return {"status": "archived", "note": note, **values}, outcome
    if live_ok:
        return ({"status": "live", "note": f"{found}: can't be checked", "captured_at": None},
                "kept the original host's copy, not archived")
    gone = {"data": None, "size": None, "content_type": None, "recovered_from": None,
            "captured_at": None}
    return {"status": "missing", "note": f"original host: {live}; {found}", **gone}, f"missing: {found}"


# -- all together --------------------------------------------------------------------------------

def recover_images(db: Database, throttle: Throttle, retry: bool = False,
                   limit: int | None = None) -> Counter:
    """Look for a copy of each embedded image not decided yet (resumable); `retry` looks
    again for the ones found nowhere. `limit`: at most that many images this run."""
    if retry:
        with db.transaction():
            db.execute("UPDATE embedded_images SET live = NULL, live_digest = NULL, captures = NULL, "
                       "status = NULL, note = NULL WHERE status = 'missing'")
    pending = db.query("SELECT id, url, live IS NULL, captures IS NULL FROM embedded_images "
                       "WHERE status IS NULL ORDER BY id")[:limit]
    if not pending:
        log.info("images: nothing to look for")
        return Counter()
    log.info("images: looking for %d", len(pending))
    check_live(db, [(image_id, url) for image_id, url, no_live, _ in pending if no_live])
    if not look_up_captures(db, [(image_id, url) for image_id, url, _, no_captures in pending
                                 if no_captures], throttle):
        return Counter({"left for the next run": len(pending)})
    return decide(db, [image_id for image_id, *_ in pending], throttle)


def image_counts(db: Database) -> list[tuple[str, int, int]]:
    """(status, images, uses) of every embedded image."""
    return db.query("SELECT COALESCE(status, 'not looked for yet'), COUNT(*), SUM(uses) "
                    "FROM embedded_images GROUP BY 1 ORDER BY 1")
