"""Client for the Tapatalk mobile API (XML-RPC at /mobiquo/mobiquo.php).

The HTML site sits behind a Cloudflare challenge, but the API used by the
Tapatalk apps does not, and it exposes exact counts, sticky flags, post
positions and second-precision timestamps.
"""

import http.client
import logging
import xmlrpc.client
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any
from xml.parsers.expat import ExpatError

from tapascrape.config import BoardConfig
from tapascrape.models import Forum, Post, Topic, User
from tapascrape.net.throttle import RetryableError, Throttle

log = logging.getLogger(__name__)

PAGE_SIZE = 50  # the API caps both topic and post pages at 50 items
TOPIC_MODES = ("TOP", "ANN", "")  # sticky, announcements, normal


class ApiError(Exception):
    """The API answered, but refused or reported a failure."""


class _Transport(xmlrpc.client.SafeTransport):
    user_agent = "Tapatalk"

    def __init__(self, timeout: float):
        super().__init__()
        self.timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self.timeout
        return conn


def decode(value: Any) -> Any:
    """Recursively turn xmlrpc Binary values (base64 strings) into str."""
    if isinstance(value, xmlrpc.client.Binary):
        return value.data.decode("utf-8", "replace")
    if isinstance(value, dict):
        return {k: decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [decode(v) for v in value]
    return value


def to_datetime(unix: Any) -> datetime | None:
    """API unix timestamp (str or int) -> naive UTC datetime."""
    if unix in (None, "", "0", 0):
        return None
    return datetime.fromtimestamp(int(unix), tz=timezone.utc).replace(tzinfo=None)


def to_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


class TapatalkApi:
    def __init__(self, config: BoardConfig, throttle: Throttle | None = None):
        self.config = config
        self.throttle = throttle or Throttle(config.rate)
        self._proxy = xmlrpc.client.ServerProxy(
            config.api_url, transport=_Transport(config.timeout), allow_none=True)

    # -- low level -------------------------------------------------------

    def call(self, method: str, *params: Any) -> Any:
        def attempt():
            try:
                return getattr(self._proxy, method)(*params)
            except xmlrpc.client.ProtocolError as e:
                if e.errcode == 429 or e.errcode >= 500:
                    raise RetryableError(f"HTTP {e.errcode}") from e
                raise ApiError(f"{method}: HTTP {e.errcode} {e.errmsg}") from e
            except (OSError, http.client.HTTPException) as e:
                raise RetryableError(repr(e)) from e
            except ExpatError as e:
                # Usually an HTML error/challenge page instead of XML.
                raise RetryableError(f"malformed response: {e}") from e

        what = f"{method}{params}"
        result = decode(self.throttle.call(attempt, self.config.max_retries, what))
        if isinstance(result, dict) and result.get("result") is False:
            raise ApiError(f"{method}: {result.get('result_text') or 'failed'}")
        return result

    # -- board -----------------------------------------------------------

    def board_stats(self) -> dict:
        return self.call("get_board_stat")

    def forum_tree(self) -> list[Forum]:
        """Root forums with their `children` populated recursively."""
        def build(raw: dict, parent_id: int | None) -> Forum:
            forum = Forum(
                id=int(raw["forum_id"]),
                parent_id=parent_id,
                name=raw.get("forum_name", ""),
                description=raw.get("description", ""),
                is_category=bool(raw.get("sub_only")),
            )
            forum.children = [build(c, forum.id) for c in raw.get("child", [])]
            return forum

        return [build(raw, None) for raw in self.call("get_forum", True)]

    # -- topics ----------------------------------------------------------

    def topic_page(self, forum_id: int, start: int, mode: str = "") -> dict:
        end = start + PAGE_SIZE - 1
        if mode:
            return self.call("get_topic", str(forum_id), start, end, mode)
        return self.call("get_topic", str(forum_id), start, end)

    def topic_total(self, forum_id: int) -> int:
        """Number of topics in a forum (sticky + announcements + normal)."""
        return sum(int(self.topic_page(forum_id, 0, mode).get("total_topic_num", 0))
                   for mode in TOPIC_MODES)

    def iter_topics(self, forum_id: int) -> Iterator[Topic]:
        """All topics that belong to `forum_id`, stickies first, each once."""
        seen: set[int] = set()
        for mode in TOPIC_MODES:
            start = 0
            while True:
                page = self.topic_page(forum_id, start, mode)
                raws = page.get("topics", [])
                for raw in raws:
                    topic = self._topic(raw, sticky_listing=(mode == "TOP"))
                    # Moved-topic stubs and global announcements can point elsewhere.
                    if topic is None or topic.forum_id != forum_id or topic.id in seen:
                        continue
                    seen.add(topic.id)
                    yield topic
                start += PAGE_SIZE
                if not raws or start >= int(page.get("total_topic_num", 0)):
                    break

    @staticmethod
    def _topic(raw: dict, sticky_listing: bool) -> Topic | None:
        if raw.get("is_moved"):
            return None
        topic_id = to_int(raw.get("real_topic_id")) or int(raw["topic_id"])
        return Topic(
            id=topic_id,
            forum_id=int(raw["forum_id"]),
            user_id=to_int(raw.get("topic_author_id")),
            name=raw.get("topic_title", ""),
            stickied=sticky_listing or bool(raw.get("is_sticky")),
            locked=bool(raw.get("is_closed")),
            post_count=int(raw.get("total_post_num") or int(raw.get("reply_number", 0)) + 1),
            view_count=int(raw.get("view_number", 0)),
            author_name=raw.get("topic_author_name") or None,
        )

    # -- posts -----------------------------------------------------------

    def post_range(self, topic_id: int, start: int, end: int) -> tuple[int, list[Post]]:
        """(total post count, posts at offsets start..end inclusive) of a topic.

        At most PAGE_SIZE posts per call. Each call bumps the topic's view count.
        """
        page = self.call("get_thread", str(topic_id), start, end, True)
        posts = [
            Post(
                id=int(raw["post_id"]),
                topic_id=topic_id,
                user_id=to_int(raw.get("post_author_id")),
                index=int(raw.get("position") or start + i + 1),
                timestamp=to_datetime(raw.get("timestamp")),
                content=raw.get("post_content", ""),
                author_name=raw.get("post_author_name") or None,
            )
            for i, raw in enumerate(page.get("posts", []))
        ]
        return int(page.get("total_post_num", 0)), posts

    def iter_posts(self, topic_id: int) -> Iterator[Post]:
        """All posts of a topic in order."""
        start = 0
        while True:
            total, posts = self.post_range(topic_id, start, start + PAGE_SIZE - 1)
            yield from posts
            start += PAGE_SIZE
            if not posts or start >= total:
                break

    # -- users -----------------------------------------------------------

    def get_user(self, user_id: int) -> User:
        raw = self.call("get_user_info", xmlrpc.client.Binary(b""), str(user_id))
        return User(
            id=int(raw["user_id"]),
            name=raw.get("username") or raw.get("user_name", ""),
            joined_at=to_datetime(raw.get("timestamp_reg")),
            last_active_at=to_datetime(raw.get("timestamp")),
            post_count=to_int(raw.get("post_count")),
            avatar_url=raw.get("icon_url") or None,
            group_ids=[int(g) for g in raw.get("usergroup_id", [])],
        )
