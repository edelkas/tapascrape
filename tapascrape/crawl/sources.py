"""Post BBCode sources -> posts.source (needs a logged-in session).

get_thread strips most BBCode ([color], [size], [list], ...), but the Quote
feature returns the post's source untouched. Posts are fetched in batches;
a batch that fails (a post the server can't process, see crawl.posts) or can't
be split back into posts is halved until the culprits are isolated. Those are
recorded as "source-gap:<post id>" and skipped on later runs.

Resumable without extra state: it only fetches posts whose source is NULL.
"""

import logging

from tapascrape.crawl.posts import SERVER_SQL_ERROR
from tapascrape.crawl.progress import Progress
from tapascrape.db.base import Database
from tapascrape.net.api import ApiError, SplitError, TapatalkApi

log = logging.getLogger(__name__)

GAP_PREFIX = "source-gap:"


def pending_sources(db: Database, forum_ids: list[int] | None = None) -> list[int]:
    sql = "SELECT p.id FROM posts p"
    params: list[int] = []
    if forum_ids is not None:
        sql += (" JOIN topics t ON t.id = p.topic_id "
                f"WHERE t.forum_id IN ({', '.join('?' for _ in forum_ids)}) AND")
        params = list(forum_ids)
    else:
        sql += " WHERE"
    gaps = {int(k) for k in db.states(GAP_PREFIX)}
    return [r[0] for r in db.query(sql + " p.source IS NULL ORDER BY p.id", params)
            if r[0] not in gaps]


def fetch_sources(api: TapatalkApi, post_ids: list[int],
                  gaps: list[tuple[int, str]]) -> dict[int, str]:
    """Sources of `post_ids`, halving on failures; unfetchable ids go to `gaps`."""
    try:
        return api.quote_posts(post_ids)
    except (ApiError, SplitError) as e:
        if isinstance(e, ApiError) and SERVER_SQL_ERROR not in str(e):
            raise
        if len(post_ids) == 1:
            gaps.append((post_ids[0], str(e)))
            return {}
    mid = len(post_ids) // 2
    return {**fetch_sources(api, post_ids[:mid], gaps), **fetch_sources(api, post_ids[mid:], gaps)}


def crawl_sources(api: TapatalkApi, db: Database, forum_ids: list[int] | None = None,
                  batch_size: int = 100) -> int:
    todo = pending_sources(db, forum_ids)
    if not todo:
        log.info("sources: nothing to do")
        return 0
    log.info("sources: %d posts pending", len(todo))
    progress = Progress("sources", len(todo))
    stored = 0
    for start in range(0, len(todo), batch_size):
        batch = todo[start:start + batch_size]
        gaps: list[tuple[int, str]] = []
        sources = fetch_sources(api, batch, gaps)
        with db.transaction():
            db.update_many("posts", [{"id": pid, "source": src} for pid, src in sources.items()])
            for post_id, error in gaps:
                log.warning("post %d: no source available (%s)", post_id, error.splitlines()[0])
                db.set_state(f"{GAP_PREFIX}{post_id}", error[:1000])
        stored += len(sources)
        progress.advance(len(batch))
    log.info("sources: %d stored", stored)
    return stored
