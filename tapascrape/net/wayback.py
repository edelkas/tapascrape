"""Wayback Machine (web.archive.org) lookups for files from dead hosts."""

import json
import logging
import urllib.parse
from dataclasses import dataclass

from tapascrape.net.http import fetch_bytes
from tapascrape.net.throttle import Throttle

log = logging.getLogger(__name__)

CDX_URL = "https://web.archive.org/cdx/search/cdx"
# archive.org rate-limits hard (HTTP 429); back off for up to a few minutes.
MAX_RETRIES = 7
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
    body = fetch_bytes(f"{CDX_URL}?{query}", throttle, timeout=120, max_retries=MAX_RETRIES)
    if not body:
        return []
    rows = json.loads(body) if body.strip() else []
    return [(row[0], row[1]) for row in rows[1:]]  # the first row is the header


def fetch_archived_image(url: str, throttle: Throttle) -> ArchivedFile | None:
    """The first archived capture of `url` that really is an image, or None."""
    for timestamp, original in snapshots(url, throttle):
        # "id_" asks for the file as it was captured, without the Wayback toolbar.
        archived = f"https://web.archive.org/web/{timestamp}id_/{urllib.parse.quote(original, safe=URL_SAFE)}"
        data = fetch_bytes(archived, throttle, timeout=60, max_retries=MAX_RETRIES)
        if data and (mime := image_type(data)):
            return ArchivedFile(data, mime, archived)
        log.debug("%s: capture %s is not an image", url, timestamp)
    return None
