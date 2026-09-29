"""Topic threads -> posts table. Resumable page by page."""

import logging

from tapascrape.crawl.progress import Progress
from tapascrape.crawl.topics import seed_users
from tapascrape.db.base import Database
from tapascrape.net.api import ApiError, TapatalkApi

log = logging.getLogger(__name__)

STATE_PREFIX = "topic-posts:"  # value: "done", or the next offset to fetch
DONE = "done"


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
    for topic_id, start, _ in todo:
        try:
            for next_start, posts in api.iter_post_pages(topic_id, start):
                with db.transaction():
                    db.upsert_many("posts", [
                        {"id": p.id, "topic_id": p.topic_id, "user_id": p.user_id, "index": p.index,
                         "timestamp": p.timestamp, "content": p.content}
                        for p in posts])
                    db.upsert_many("users", seed_users((p.user_id, p.author_name) for p in posts))
                    db.set_state(f"{STATE_PREFIX}{topic_id}", str(next_start))
                total += len(posts)
                progress.advance(len(posts), detail=f"topic {topic_id}")
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
