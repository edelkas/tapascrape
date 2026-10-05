"""Wayback Machine (web.archive.org) lookups for files from dead hosts."""

import json
import logging
import urllib.parse
from dataclasses import dataclass
from email.message import Message

from tapascrape.net.http import fetch, fetch_bytes
from tapascrape.net.throttle import Throttle

log = logging.getLogger(__name__)

CDX_URL = "https://web.archive.org/cdx/search/cdx"
# archive.org rate-limits hard (HTTP 429); back off for up to a few minutes.
MAX_RETRIES = 7
# An honest User-Agent. archive.org answers a browser's User-Agent sent with Python's
# TLS fingerprint with 429 "suspected abusive bot traffic", every time.
USER_AGENT = "tapascrape (forum archiver; Python-urllib)"
# Characters kept as they are when building the raw-snapshot URL.
URL_SAFE = ":/?&=%;()!,.~-_+@*'$"

IMAGE_SIGNATURES = {
    b"GIF87a": "image/gif",
    b"GIF89a": "image/gif",
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"BM": "image/bmp",
}


def image_type(data: bytes) -> str | None:
    """MIME type of an image from its first bytes (archived error pages aren't images)."""
    for signature, mime in IMAGE_SIGNATURES.items():
        if data.startswith(signature):
            return mime
    return None


@dataclass
class ArchivedFile:
    data: bytes
    content_type: str
    archived_url: str


def snapshots(url: str, throttle: Throttle, limit: int = 5) -> list[tuple[str, str]]:
    """(timestamp, original URL) of captures of `url` archived as images with HTTP 200."""
    target = url.split("://", 1)[-1]
    query = urllib.parse.urlencode([
        ("url", target), ("output", "json"), ("fl", "timestamp,original"),
        ("filter", "statuscode:200"), ("filter", "mimetype:image/.*"), ("limit", str(limit)),
    ])
    body = fetch_bytes(f"{CDX_URL}?{query}", throttle, timeout=120, max_retries=MAX_RETRIES,
                           user_agent=USER_AGENT)
    if not body:
        return []
    rows = json.loads(body) if body.strip() else []
    return [(row[0], row[1]) for row in rows[1:]]  # the first row is the header


def fetch_archived_image(url: str, throttle: Throttle) -> ArchivedFile | None:
    """The first archived capture of `url` that really is an image, or None."""
    for timestamp, original in snapshots(url, throttle):
        # "id_" asks for the file as it was captured, without the Wayback toolbar.
        archived = f"https://web.archive.org/web/{timestamp}id_/{urllib.parse.quote(original, safe=URL_SAFE)}"
        data = fetch_bytes(archived, throttle, timeout=60, max_retries=MAX_RETRIES,
                           user_agent=USER_AGENT)
        if data and (mime := image_type(data)):
            return ArchivedFile(data, mime, archived)
        log.debug("%s: capture %s is not an image", url, timestamp)
    return None


@dataclass
class Capture:
    original: str   # the URL as archived
    timestamp: str
    mimetype: str


def prefix_captures(prefix: str, throttle: Throttle, page_size: int = 50000,
                    original: str | None = None) -> list[Capture]:
    """Every capture archived with HTTP 200 of the URLs starting with `prefix`.

    One query lists a whole directory, instead of a lookup per file. `original`
    (a regex over the whole archived URL) narrows it down on archive.org's side.
    """
    captures: list[Capture] = []
    resume = None
    while True:
        params = [("url", prefix.split("://", 1)[-1]), ("matchType", "prefix"), ("output", "json"),
                  ("fl", "original,timestamp,mimetype"), ("filter", "statuscode:200"),
                  ("limit", str(page_size)), ("showResumeKey", "true")]
        if original:
            params.append(("filter", f"original:{original}"))
        if resume:
            params.append(("resumeKey", resume))
        body = fetch_bytes(f"{CDX_URL}?{urllib.parse.urlencode(params)}", throttle, timeout=300,
                           max_retries=MAX_RETRIES, user_agent=USER_AGENT)
        rows = json.loads(body) if body and body.strip() else []
        resume = None
        if len(rows) >= 2 and len(rows[-1]) == 1:  # [..., [], [resume key]]
            resume = rows[-1][0]
            rows = rows[:-2]
        captures += [Capture(*row) for row in rows[1:] if len(row) == 3]
        if not resume:
            return captures


def fetch_capture(capture: Capture, throttle: Throttle) -> tuple[bytes, Message, str] | None:
    """(data, headers, archived URL) of a capture, as it was archived (no Wayback toolbar).

    Headers of the original response come prefixed with "x-archive-orig-".
    """
    archived = (f"https://web.archive.org/web/{capture.timestamp}id_/"
                f"{urllib.parse.quote(capture.original, safe=URL_SAFE)}")
    found = fetch(archived, throttle, timeout=120, max_retries=MAX_RETRIES, user_agent=USER_AGENT)
    return (found[0], found[1], archived) if found is not None else None
