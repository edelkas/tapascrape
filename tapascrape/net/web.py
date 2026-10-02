"""HTML client for the board's website, which sits behind a Cloudflare challenge.

Plain HTTP clients get a JavaScript challenge, and even curl_cffi (Chrome's TLS
fingerprint) is challenged unless it carries a `cf_clearance` cookie obtained
by a real browser with the same User-Agent. Strategy:

1. curl_cffi, optionally seeded with cookies.txt exported from your browser
   (--cookies, with that browser's --user-agent);
2. when challenged (and the browser is allowed), open a visible Chrome window
   via zendriver. If Cloudflare challenges it, it usually passes on its own
   (otherwise click the checkbox), and its cookies are copied to curl_cffi;
3. if curl_cffi is still challenged (Cloudflare doesn't always hand out a
   clearance cookie), keep fetching pages through that Chrome window.

Pages are returned whatever their HTTP status (phpBB error pages such as
"user does not exist" are regular HTML); parsers decide what they mean.
"""

import asyncio
import http.cookiejar
import logging
import time
from pathlib import Path

from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException

from tapascrape.config import BoardConfig
from tapascrape.net.session import Session, board_user_id
from tapascrape.net.throttle import RetryableError, Throttle

log = logging.getLogger(__name__)

CHALLENGE_TIMEOUT = 180  # seconds to wait for a challenge to be passed in the browser
PAGE_TIMEOUT = 60


class CloudflareError(Exception):
    """The challenge couldn't be passed with the available means."""


def is_challenge(text: str) -> bool:
    return "_cf_chl_opt" in text or "<title>Just a moment...</title>" in text


class WebClient:
    def __init__(self, config: BoardConfig, throttle: Throttle | None = None,
                 cookies_file: str | None = None, user_agent: str | None = None,
                 use_browser: bool = True, impersonate: str = "chrome",
                 login: Session | None = None, profile_dir: Path | None = None):
        """`login`/`profile_dir`: a saved logged-in session and its Chrome profile."""
        self.config = config
        self.throttle = throttle or Throttle(config.rate)
        self.use_browser = use_browser
        self.profile_dir = profile_dir
        self.session = requests.Session(impersonate=impersonate, timeout=config.timeout)
        self._browser: _Browser | None = None
        self._via_browser = False  # set once curl_cffi can't get through
        if login is not None:
            user_agent = user_agent or login.user_agent
            for name, value in login.cookies.items():
                self.session.cookies.set(name, value, domain=".tapatalk.com")
        if user_agent:
            self.session.headers["User-Agent"] = user_agent
        if cookies_file:
            jar = http.cookiejar.MozillaCookieJar(cookies_file)
            jar.load(ignore_discard=True, ignore_expires=True)
            for cookie in jar:
                self.session.cookies.set(cookie.name, cookie.value, domain=cookie.domain)
            if not user_agent:
                log.warning("--cookies without --user-agent: cf_clearance only works "
                            "with the User-Agent of the browser that obtained it")

    def get(self, path: str) -> str:
        """HTML of a board page (path relative to the board URL)."""
        url = path if path.startswith("http") else self.config.base_url + path
        if not self._via_browser:
            text = self._curl_get(url)
            if not is_challenge(text):
                return text
            if not self.use_browser:
                raise CloudflareError(
                    "Cloudflare challenge: export fresh cookies (cookies.txt) from a browser that "
                    "can open the board; pass them with --cookies and that browser's --user-agent")
            self._borrow_browser_cookies()
            text = self._curl_get(url)
            if not is_challenge(text):
                return text
            log.info("curl_cffi is still challenged; fetching pages through Chrome from now on")
            self._via_browser = True
        return self.throttle.call(lambda: self._browser.get(url), self.config.max_retries, f"GET {url}")

    def _curl_get(self, url: str) -> str:
        def attempt() -> str:
            log.debug("GET %s (curl_cffi)", url)
            try:
                response = self.session.get(url)
            except RequestException as e:
                raise RetryableError(repr(e)) from e
            if response.status_code == 429 or (
                    response.status_code >= 500 and not is_challenge(response.text)):
                raise RetryableError(f"HTTP {response.status_code}")
            return response.text

        return self.throttle.call(attempt, self.config.max_retries, f"GET {url}")

    def _borrow_browser_cookies(self) -> None:
        if self._browser is None:
            log.info("Cloudflare challenge: opening Chrome "
                     "(if a checkbox appears in the window, click it)")
            self._browser = _Browser(self.profile_dir)
        user_agent, cookies = self._browser.clearance(self.config.base_url)
        self.session.headers["User-Agent"] = user_agent
        for name, value, domain in cookies:
            self.session.cookies.set(name, value, domain=domain)

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
            self._browser = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def browser_login(config: BoardConfig, profile_dir: Path, timeout: float = 600) -> Session:
    """Open the login page in Chrome and wait for the user to log in by hand."""
    browser = _Browser(profile_dir)
    try:
        browser.get(config.base_url + "ucp.php?mode=login")
        log.info("Log in to the board in the Chrome window (any method); waiting up to %d min",
                 timeout // 60)
        deadline = time.monotonic() + timeout
        while True:
            try:
                user_agent, cookies = browser.state()
            except Exception:  # the tab is mid-navigation
                time.sleep(2)
                continue
            # Only the board's own cookies (the browser also holds ad trackers etc.).
            flat = {name: value for name, value, domain in cookies
                    if domain.lstrip(".").endswith("tapatalk.com")}
            user_id = board_user_id(config.board, flat)
            if user_id is not None:
                return Session(user_agent=user_agent, cookies=flat, user_id=user_id)
            if time.monotonic() > deadline:
                raise TimeoutError("not logged in before the timeout")
            time.sleep(2)
    finally:
        browser.close()


class _Browser:
    """A visible Chrome window driven by zendriver, used synchronously.

    With `profile_dir`, Chrome keeps its profile (and so a login) there;
    otherwise it starts from a throwaway profile.
    """

    def __init__(self, profile_dir: Path | None = None):
        try:
            import zendriver
        except ImportError as e:
            raise CloudflareError(
                "zendriver is needed to pass Cloudflare automatically "
                "(pip install zendriver), or use --cookies/--user-agent") from e
        self._loop = asyncio.new_event_loop()
        options = {}
        if profile_dir is not None:
            profile_dir.mkdir(parents=True, exist_ok=True)
            options["user_data_dir"] = str(profile_dir)
        # Headless Chrome doesn't pass the challenge; a visible window does.
        self._browser = self._run(zendriver.start(headless=False, **options))
        self._tab = None

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    def clearance(self, url: str) -> tuple[str, list[tuple[str, str, str]]]:
        """Load `url` until it's past any challenge; return (User-Agent, cookies)."""
        self.get(url)
        return self.state()

    def state(self) -> tuple[str, list[tuple[str, str, str]]]:
        """(User-Agent, [(name, value, domain)]) of the browser right now."""
        user_agent = self._run(self._tab.evaluate("navigator.userAgent"))
        cookies = self._run(self._browser.cookies.get_all())
        return user_agent, [(c.name, c.value, c.domain) for c in cookies]

    def get(self, url: str) -> str:
        return self._run(self._get(url))

    async def _get(self, url: str) -> str:
        log.debug("GET %s (Chrome)", url)
        if self._tab is None:
            self._tab = await self._browser.get(url)
        else:
            await self._tab.get(url)
        start = time.monotonic()
        warned = False
        while True:
            state = await self._tab.evaluate("document.readyState")
            html = await self._tab.get_content()
            if state == "complete" and not is_challenge(html):
                return html
            waited = time.monotonic() - start
            if is_challenge(html):
                if waited > CHALLENGE_TIMEOUT:
                    raise CloudflareError("timed out waiting for the Cloudflare challenge")
                if waited > 15 and not warned:
                    log.warning("still on the Cloudflare challenge: click the checkbox in Chrome")
                    warned = True
            elif waited > PAGE_TIMEOUT:
                raise RetryableError(f"page load timed out: {url}")
            await asyncio.sleep(0.5)

    def close(self) -> None:
        try:
            self._run(self._browser.stop())
        finally:
            self._loop.close()
