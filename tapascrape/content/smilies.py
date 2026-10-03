"""Smileys embedded as images -> the `smilies` table.

Yuku and forumer (and, for a handful, other Invision boards) served the
board's old smileys; posts reference them as [img]<url>[/img], and those
hosts are gone. A few more live on Tapatalk itself, under the board's
forum_data/.../smilies/ (referenced with board-relative URLs). Each distinct
URL gets a row with a stable id of ours, which fixed sources use
([ts:smiley=ID], see content.fix). Images are recovered from the Wayback
Machine, or downloaded from Tapatalk for the ones it still hosts.
"""

import logging
import re
from collections import Counter
from collections.abc import Callable
from pathlib import PurePosixPath
from urllib.parse import unquote

from tapascrape.content.scan import iter_signature_sources, iter_sources
from tapascrape.db.base import Database
from tapascrape.net.throttle import RetryableError, Throttle
from tapascrape.net.wayback import ArchivedFile, fetch_archived_image, image_type
from tapascrape.net.web import WebClient

log = logging.getLogger(__name__)

MISSING_PREFIX = "smiley-missing:"  # crawl_state: no image found anywhere
TAPATALK = "https://www.tapatalk.com/groups/"
# Yuku's smileys; Invision boards' (forumer's and a few others): /html/emoticons/,
# /style_emoticons/; Tapatalk-hosted ones: forum_data/forums.me/meta/<board>/smilies/.
SMILEY_PATH = (r"(?:static\.yuku\.com(?::\d+)?/domain/bypass/images/|/+html/emoticons/"
               r"|/style_emoticons/|forum_data/(?:[^\s\[\]'/]+/)*smilies/)")
# [img]url[/img], with or without Yuku's quotes around the URL.
SMILEY_IMG = re.compile(r"\[img\]'?((?:https?://)?[^\[\]'\s]*?" + SMILEY_PATH + r"[^\[\]'\s]*?)'?\[/img\]",
                        re.IGNORECASE)
# Board-relative Tapatalk URL; the board's name is part of the path.
FORUM_DATA = re.compile(r"/?(forum_data/forums\.me/meta/([^/]+)/.*)")


def normalize_url(url: str) -> str:
    """One spelling per smiley: absolute, lowercase scheme and host, no default port."""
    if relative := FORUM_DATA.fullmatch(url):
        return f"{TAPATALK}{relative.group(2)}/{relative.group(1)}"
    scheme, sep, rest = url.partition("://")
    if not sep:
        scheme, rest = "http", url
    host, slash, path = rest.partition("/")
    host = host.lower().removesuffix(":80")
    return f"{scheme.lower()}://{host}{slash}{path}"


def describe(url: str) -> tuple[str, str]:
    """(name, host kind) of a smiley URL."""
    name = PurePosixPath(unquote(url.split("?", 1)[0])).stem
    host = url.split("://", 1)[-1].split("/", 1)[0]
    kind = ("yuku" if "yuku.com" in host else "forumer" if host.endswith("forumer.com")
            else "tapatalk" if host.endswith("tapatalk.com") else "other")
    return name, kind


def tapatalk_board(url: str) -> str | None:
    """The board a Tapatalk-hosted smiley belongs to."""
    match = re.match(re.escape(TAPATALK) + r"([^/]+)/", url)
    return match.group(1) if match else None


def smiley_ids(db: Database) -> dict[str, int]:
    """Normalized URL -> smiley id."""
    return {url: smiley_id for smiley_id, url in db.query("SELECT id, url FROM smilies")}


def collect_smilies(db: Database) -> int:
    """Add a row for every smiley URL used in posts and signatures; refresh use counts.

    Existing rows keep their ids; new URLs get the next ids, most used first.
    Returns the number of new smileys.
    """
    uses: Counter = Counter()
    for texts in (iter_sources(db), iter_signature_sources(db)):
        for _, text in texts:
            for match in SMILEY_IMG.finditer(text):
                uses[normalize_url(match.group(1))] += 1
    known = smiley_ids(db)
    next_id = max(known.values(), default=0) + 1
    rows = []
    for url, count in sorted(uses.items(), key=lambda item: (-item[1], item[0])):
        if url in known:
            rows.append({"id": known[url], "uses": count})
            continue
        name, host = describe(url)
        rows.append({"id": next_id, "url": url, "name": name, "host": host, "uses": count})
        next_id += 1
    with db.transaction():
        db.update_many("smilies", [r for r in rows if "url" not in r])
        db.upsert_many("smilies", [r for r in rows if "url" in r])
    added = sum("url" in r for r in rows)
    log.info("smilies: %d distinct (%d new), %d uses", len(uses), added, sum(uses.values()))
    return added


def pending_smilies(db: Database, retry: bool = False) -> list[tuple[int, str, str]]:
    """(id, url, host kind) of smileys without an image still worth looking for."""
    missing = set() if retry else {int(k) for k in db.states(MISSING_PREFIX)}
    return [row for row in db.query("SELECT id, url, host FROM smilies WHERE data IS NULL ORDER BY id")
            if row[0] not in missing]


# No image/webp: Cloudflare would answer with a WebP conversion instead of the file itself.
ORIGINAL_IMAGES = "image/gif,image/png,image/jpeg;q=0.9,*/*;q=0.5"


def tapatalk_fetcher(web: WebClient) -> Callable[[str], ArchivedFile | None]:
    """Downloads Tapatalk-hosted smileys through the board's website client."""
    def fetch(url: str) -> ArchivedFile | None:
        data = web.get_bytes(url, accept=ORIGINAL_IMAGES, original=True)
        mime = image_type(data) if data else None
        if data and mime is None:
            log.warning("%s: not a GIF/PNG/JPEG file; skipped", url)
        return ArchivedFile(data, mime, url) if mime else None
    return fetch


def recover_smilies(db: Database, throttle: Throttle, retry: bool = False,
                    live: Callable[[str], ArchivedFile | None] | None = None) -> int:
    """Download the images of smileys without data.

    Tapatalk-hosted ones are fetched with `live` (from the board's website);
    everything else, and Tapatalk ones that are gone, from the Wayback Machine.
    """
    if retry:
        with db.transaction():
            for key in db.states(MISSING_PREFIX):
                db.delete_state(f"{MISSING_PREFIX}{key}")
    todo = pending_smilies(db)
    if not todo:
        log.info("smilies: nothing to recover")
        return 0
    recovered = 0
    for smiley_id, url, host in todo:
        try:
            found = live(url) if host == "tapatalk" and live is not None else None
            if found is None:
                found = fetch_archived_image(url, throttle)
        except RetryableError as e:
            log.warning("smiley %d (%s): %s; will retry on the next run", smiley_id, url, e)
            continue
        with db.transaction():
            if found is None:
                db.set_state(f"{MISSING_PREFIX}{smiley_id}", url)
            else:
                db.update_many("smilies", [{"id": smiley_id, "data": found.data,
                                            "content_type": found.content_type,
                                            "recovered_from": found.archived_url}])
        if found is None:
            log.warning("smiley %d: not found (%s)", smiley_id, url)
        else:
            log.info("smiley %d: recovered %s (%d bytes)", smiley_id, url, len(found.data))
            recovered += 1
    log.info("smilies: %d recovered, %d not found anywhere",
             recovered, len(db.states(MISSING_PREFIX)))
    return recovered
