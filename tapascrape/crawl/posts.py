"""Topic threads -> posts table. Resumable page by page."""

import logging

from tapascrape.crawl.progress import Progress
from tapascrape.crawl.topics import seed_users
from tapascrape.db.base import Database
from tapascrape.models import Post
from tapascrape.net.api import PAGE_SIZE, ApiError, TapatalkApi

log = logging.getLogger(__name__)

STATE_PREFIX = "topic-posts:"  # value: "done", or the next offset to fetch
GAP_PREFIX = "post-gap:"  # "post-gap:<topic>:<offset>" -> server error for an unfetchable post
DONE = "done"
SERVER_SQL_ERROR = "SQL ERROR"  # marker of a server-side phpBB database error


def pending_topics(db: Database, forum_ids: list[int] | None) -> list[tuple[int, int, int]]:
    """(topic_id, resume_offset, expected_posts) for topics whose posts aren't complete."""
    sql = "SELECT id, post_count FROM topics"
    params: list[int] = []
    if forum_ids is not None:
        sql += f" WHERE forum_id IN ({', '.join('?' for _ in forum_ids)})"
        params = list(forum_ids)
    state = db.states(STATE_PREFIX)
    todo = []
    for topic_id, post_count in db.query(sql + " ORDER BY id", params):
        value = state.get(str(topic_id))
        if value != DONE:
            todo.append((topic_id, int(value or 0), post_count or 0))
    return todo


def fetch_range(api: TapatalkApi, topic_id: int, start: int, end: int,
                gaps: list[tuple[int, str]]) -> tuple[int | None, list[Post]]:
    """Like api.post_range, but works around posts the server can't render.

    Some posts make Tapatalk's own backend fail (e.g. an author name with a
    4-byte character trips a MySQL collation error), taking the whole range
    down with them. Split the range until the bad posts are isolated, and
    record their offsets in `gaps`. Returns (total or None if unknown, posts).
    """
    try:
        return api.post_range(topic_id, start, end)
    except ApiError as e:
        if SERVER_SQL_ERROR not in str(e):
            raise
        if start == end:
            gaps.append((start, str(e)))
            return None, []
    mid = (start + end) // 2
    total_a, posts_a = fetch_range(api, topic_id, start, mid, gaps)
    total_b, posts_b = fetch_range(api, topic_id, mid + 1, end, gaps)
    return total_a or total_b, posts_a + posts_b


def crawl_posts(api: TapatalkApi, db: Database, forum_ids: list[int] | None = None) -> int:
    todo = pending_topics(db, forum_ids)
    if not todo:
        log.info("posts: nothing to do")
        return 0
    expected = sum(max(n - start, 0) for _, start, n in todo)
    log.info("posts: %d topics pending, ~%d posts", len(todo), expected)
    progress = Progress("posts", expected)
    total = 0
    failed = 0
    for topic_id, start, topic_total in todo:
        try:
            while True:
                gaps: list[tuple[int, str]] = []
                end = start + PAGE_SIZE - 1
                if topic_total > start:
                    end = min(end, topic_total - 1)  # don't bisect past the last post
                page_total, posts = fetch_range(api, topic_id, start, end, gaps)
                topic_total = page_total if page_total is not None else topic_total
                start += PAGE_SIZE
                with db.transaction():
                    db.upsert_many("posts", [
                        {"id": p.id, "topic_id": p.topic_id, "user_id": p.user_id, "index": p.index,
                         "timestamp": p.timestamp, "content": p.content}
                        for p in posts])
                    db.upsert_many("users", seed_users((p.user_id, p.author_name) for p in posts))
                    for offset, error in gaps:
                        log.warning("topic %d post #%d: server can't return it; skipped (%s)",
                                    topic_id, offset + 1, error.splitlines()[0])
                        db.set_state(f"{GAP_PREFIX}{topic_id}:{offset}", error[:1000])
                    db.set_state(f"{STATE_PREFIX}{topic_id}", str(start))
                total += len(posts)
                progress.advance(len(posts) + len(gaps), detail=f"topic {topic_id}")
                if not (posts or gaps) or start >= topic_total:
                    break
        except ApiError as e:
            # Stays pending at its last offset (retried next run); don't stop the crawl.
            log.warning("topic %d: %s", topic_id, e)
            failed += 1
        else:
            db.set_state(f"{STATE_PREFIX}{topic_id}", DONE)
        db.commit()
    if failed:
        log.warning("posts: %d topics failed; rerun to retry them", failed)
    return total
