"""Plain HTTP downloads (avatars on the Tapatalk CDN, which isn't challenged)."""

import http.client
import urllib.error
import urllib.request

from tapascrape.net.throttle import RetryableError, Throttle

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def fetch_bytes(url: str, throttle: Throttle, timeout: float = 30.0,
                max_retries: int = 3) -> bytes | None:
    """GET `url`; None if it doesn't exist (4xx)."""
    def attempt() -> bytes | None:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                raise RetryableError(f"HTTP {e.code}") from e
            return None
        except (OSError, http.client.HTTPException) as e:
            raise RetryableError(repr(e)) from e

    return throttle.call(attempt, max_retries, f"GET {url}")
