"""Plain HTTP downloads (avatars on the Tapatalk CDN, which isn't challenged)."""

import http.client
import logging
import re
import urllib.error
import urllib.request
import uuid
from email.message import Message

from tapascrape.net.throttle import RetryableError, Throttle

log = logging.getLogger(__name__)

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
# No image/webp: Cloudflare would answer with a WebP conversion instead of the file itself.
ORIGINAL_IMAGES = "image/gif,image/png,image/jpeg;q=0.9,*/*;q=0.5"


def retry_after(value: str | None) -> float | None:
    """Seconds from a Retry-After header (the HTTP-date form is ignored)."""
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def _reason(error: urllib.error.HTTPError) -> str:
    """The start of an error page's text, which tells a block from a rate limit."""
    try:
        body = error.read(400).decode("utf-8", "replace")
    except (OSError, http.client.HTTPException):
        return ""
    text = " ".join(re.sub(r"<[^>]*>", " ", body).split())
    return f": {text[:150]}" if text and not body.lstrip().startswith("<!") else ""


def cache_busted(url: str) -> str:
    """`url` with a unique query parameter, so a CDN cache can't answer it.

    Cloudflare's cache serves images "polished" (recompressed, and lossy:
    animated GIFs lose frames); a cache miss is answered with the file itself.
    """
    return url + ("&" if "?" in url else "?") + f"nocache={uuid.uuid4().hex}"


def fetch_bytes(url: str, throttle: Throttle, timeout: float = 30.0,
                max_retries: int = 3, original: bool = False,
                user_agent: str = USER_AGENT) -> bytes | None:
    """GET `url`; None if it doesn't exist (4xx).

    `original`: for images behind Cloudflare: bypass its cache and refuse a
    polished answer, so the file is stored as it was uploaded.
    """
    found = fetch(url, throttle, timeout, max_retries, original, user_agent)
    return found[0] if found is not None else None


def fetch(url: str, throttle: Throttle, timeout: float = 30.0, max_retries: int = 3,
          original: bool = False, user_agent: str = USER_AGENT) -> tuple[bytes, Message] | None:
    """Like fetch_bytes, with the response headers."""
    def attempt() -> tuple[bytes, Message] | None:
        target = cache_busted(url) if original else url
        log.debug("GET %s", target)
        headers = {"User-Agent": user_agent}
        if original:
            headers["Accept"] = ORIGINAL_IMAGES
        request = urllib.request.Request(target, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if original and response.headers.get("cf-polished"):
                    raise RetryableError(f"got Cloudflare's polished copy "
                                         f"({response.headers['cf-polished']})")
                return response.read(), response.headers
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                raise RetryableError(f"HTTP {e.code}{_reason(e)}",
                                     retry_after(e.headers.get("Retry-After"))) from e
            return None
        except (OSError, http.client.HTTPException) as e:
            raise RetryableError(repr(e)) from e

    return throttle.call(attempt, max_retries, f"GET {url}")
