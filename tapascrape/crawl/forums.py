"""Forum tree: storing it, and the dry-run survey that prints it."""

import logging
from dataclasses import dataclass
from typing import TextIO

from tapascrape.db.base import Database
from tapascrape.models import Forum
from tapascrape.net.api import TapatalkApi

log = logging.getLogger(__name__)


def store_forums(db: Database, roots: list[Forum]) -> int:
    rows = [
        {"id": f.id, "parent_id": f.parent_id, "name": f.name, "description": f.description}
        for root in roots for f, _ in root.walk()
    ]
    with db.transaction():
        return db.upsert_many("forums", rows)


@dataclass
class ForumCounts:
    topics: int = 0
    posts: int | None = None  # None when posts were not counted

    def __iadd__(self, other: "ForumCounts") -> "ForumCounts":
        self.topics += other.topics
        if self.posts is not None and other.posts is not None:
            self.posts += other.posts
        else:
            self.posts = None
        return self


def survey(api: TapatalkApi, roots: list[Forum], count_posts: bool = True) -> dict[int, ForumCounts]:
    """Topic (and optionally post) counts of each forum's own topics.

    Counting posts means listing every topic (~1 request per 50 topics).
    """
    forums = [f for root in roots for f, _ in root.walk()]
    counts: dict[int, ForumCounts] = {}
    for n, forum in enumerate(forums, 1):
        if forum.is_category:
            counts[forum.id] = ForumCounts(0, 0 if count_posts else None)
            continue
        if count_posts:
            topics = list(api.iter_topics(forum.id))
            counts[forum.id] = ForumCounts(len(topics), sum(t.post_count for t in topics))
        else:
            counts[forum.id] = ForumCounts(api.topic_total(forum.id))
        log.info("[%d/%d] %s: %d topics", n, len(forums), forum.name, counts[forum.id].topics)
    return counts


def subtree_totals(forum: Forum, counts: dict[int, ForumCounts]) -> ForumCounts:
    total = ForumCounts(counts[forum.id].topics, counts[forum.id].posts)
    for child in forum.children:
        total += subtree_totals(child, counts)
    return total


def _fmt(c: ForumCounts) -> str:
    posts = "?" if c.posts is None else f"{c.posts:,}"
    return f"topics: {c.topics:,}, posts: {posts}"


def print_tree(roots: list[Forum], counts: dict[int, ForumCounts], out: TextIO) -> ForumCounts:
    """One line per forum, subforums indented. Returns the board-wide total."""
    grand = ForumCounts(0, 0)
    for root in roots:
        for forum, depth in root.walk():
            own = counts[forum.id]
            total = subtree_totals(forum, counts)
            indent = "    " * depth
            if forum.is_category:
                line = f"{indent}[{forum.id}] {forum.name} (category) — {_fmt(total)}"
            else:
                line = f"{indent}[{forum.id}] {forum.name} — {_fmt(own)}"
                if forum.children:
                    line += f"  (with subforums: {_fmt(total)})"
            print(line, file=out)
        grand += subtree_totals(root, counts)
    return grand
