"""Plain HTTP downloads (avatars on the Tapatalk CDN, which isn't challenged)."""

import http.client
import logging
import urllib.error
import urllib.request
import uuid

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


def cache_busted(url: str) -> str:
    """`url` with a unique query parameter, so a CDN cache can't answer it.

    Cloudflare's cache serves images "polished" (recompressed, and lossy:
    animated GIFs lose frames); a cache miss is answered with the file itself.
    """
    return url + ("&" if "?" in url else "?") + f"nocache={uuid.uuid4().hex}"


def fetch_bytes(url: str, throttle: Throttle, timeout: float = 30.0,
                max_retries: int = 3, original: bool = False) -> bytes | None:
    """GET `url`; None if it doesn't exist (4xx).

    `original`: for images behind Cloudflare: bypass its cache and refuse a
    polished answer, so the file is stored as it was uploaded.
    """
    def attempt() -> bytes | None:
        target = cache_busted(url) if original else url
        log.debug("GET %s", target)
        headers = {"User-Agent": USER_AGENT}
        if original:
            headers["Accept"] = ORIGINAL_IMAGES
        request = urllib.request.Request(target, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if original and response.headers.get("cf-polished"):
                    raise RetryableError(f"got Cloudflare's polished copy "
                                         f"({response.headers['cf-polished']})")
                return response.read()
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                raise RetryableError(f"HTTP {e.code}", retry_after(e.headers.get("Retry-After"))) from e
            return None
        except (OSError, http.client.HTTPException) as e:
            raise RetryableError(repr(e)) from e

    return throttle.call(attempt, max_retries, f"GET {url}")
