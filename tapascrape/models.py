"""Plain records mirroring the database tables."""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Forum:
    id: int
    parent_id: int | None
    name: str
    description: str = ""
    is_category: bool = False  # "sub_only" in the API: holds subforums, no topics
    children: list["Forum"] = field(default_factory=list, repr=False)
    # Filled in by the crawl / finalize steps.
    last_post_id: int | None = None
    post_count: int | None = None
    view_count: int | None = None

    def walk(self, depth: int = 0):
        """Yield (forum, depth) for this forum and all its descendants, pre-order."""
        yield self, depth
        for child in self.children:
            yield from child.walk(depth + 1)


@dataclass
class Topic:
    id: int
    forum_id: int
    user_id: int | None
    name: str
    stickied: bool
    locked: bool
    post_count: int  # as reported by the listing; recomputed by finalize
    view_count: int
    last_post_id: int | None = None
    created_at: datetime | None = None  # first post's timestamp; set by finalize
    author_name: str | None = None  # not a topics column; seeds users.name
    has_poll: bool = False


@dataclass
class Post:
    id: int
    topic_id: int
    user_id: int | None
    index: int
    timestamp: datetime
    content: str
    author_name: str | None = None  # not a posts column; seeds users.name


@dataclass
class User:
    id: int
    name: str
    rank: str | None = None
    joined_at: datetime | None = None
    last_active_at: datetime | None = None
    post_count: int | None = None
    signature: str | None = None
    avatar_url: str | None = None
    avatar_id: int | None = None
    group_ids: list[int] = field(default_factory=list)


@dataclass
class Group:
    id: int
    name: str | None = None
