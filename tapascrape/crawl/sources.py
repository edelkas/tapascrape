"""Post BBCode sources -> posts.source (needs a logged-in session).

get_thread strips most BBCode ([color], [size], [list], ...), but the Quote
feature returns the post's source untouched. Posts are fetched in batches;
a batch that the server refuses (some posts can't be quoted: SQL errors, or
"Need valid post id!" for posts that are readable otherwise) or that can't be
split back into posts is halved until the culprits are isolated. Those are
recorded as "source-gap:<post id>" and skipped on later runs. An expired
session (AuthError) is never treated as a bad post.

Resumable without extra state: it only fetches posts whose source is NULL.

Gaps can then be recovered from the website (recover_sources): the post's
rendered HTML is turned back into BBCode (see parse.bbcode), and the post is
tagged "source-origin:<id>" = "html" since that source is a reconstruction.
"""

import logging

from tapascrape.crawl.progress import Progress
from tapascrape.db.base import Database
from tapascrape.net.api import ApiError, AuthError, SplitError, TapatalkApi
from tapascrape.net.web import WebClient
from tapascrape.parse.bbcode import html_to_bbcode
from tapascrape.parse.topic import parse_topic_posts

log = logging.getLogger(__name__)

GAP_PREFIX = "source-gap:"
ORIGIN_PREFIX = "source-origin:"
# A batch this large in which every single post fails points to a board-wide
# problem (maintenance, rate limiting...) rather than bad posts: stop instead.
SYSTEMIC_FAILURE_BATCH = 10


class SourcesAborted(Exception):
    """Every post of a batch failed; probably not the posts' fault."""


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
    except AuthError:
        raise
    except (ApiError, SplitError) as e:
        if len(post_ids) == 1:
            gaps.append((post_ids[0], str(e)))
            return {}
    mid = len(post_ids) // 2
    return {**fetch_sources(api, post_ids[:mid], gaps), **fetch_sources(api, post_ids[mid:], gaps)}


def crawl_sources(api: TapatalkApi, db: Database, forum_ids: list[int] | None = None,
                  batch_size: int = 100, retry_gaps: bool = False) -> int:
    if retry_gaps:
        gaps = db.states(GAP_PREFIX)
        with db.transaction():
            for post_id in gaps:
                db.delete_state(f"{GAP_PREFIX}{post_id}")
        log.info("sources: retrying %d posts without a source", len(gaps))
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
        if len(batch) >= SYSTEMIC_FAILURE_BATCH and len(gaps) == len(batch):
            raise SourcesAborted(f"all {len(batch)} posts of a batch failed "
                                 f"(first error: {gaps[0][1].splitlines()[0]})")
        with db.transaction():
            db.update_many("posts", [{"id": pid, "source": src} for pid, src in sources.items()])
            for post_id, error in gaps:
                log.warning("post %d: no source available (%s)", post_id, error.splitlines()[0])
                db.set_state(f"{GAP_PREFIX}{post_id}", error[:1000])
        stored += len(sources)
        progress.advance(len(batch))
    log.info("sources: %d stored", stored)
    return stored


def recover_sources(web: WebClient, db: Database) -> int:
    """Rebuild the source of gap posts from the website; posts with a source are left alone."""
    gaps = {int(k) for k in db.states(GAP_PREFIX)}
    todo = {post_id: (topic_id, index) for post_id, topic_id, index in db.query(
        f"SELECT id, topic_id, {db.quote_ident('index')} FROM posts WHERE source IS NULL")
        if post_id in gaps}
    if not todo:
        log.info("recover sources: nothing to do")
        return 0
    log.info("recover sources: %d posts without a source", len(todo))
    recovered = 0
    for post_id, (topic_id, index) in sorted(todo.items(), key=lambda item: item[1]):
        if post_id not in todo:
            continue  # recovered from an earlier page
        # Start the page at this post; the following posts on it come along too.
        html = web.get(f"viewtopic.php?t={topic_id}&start={max(index - 1, 0)}")
        found = {p.id: p for p in parse_topic_posts(html) if p.id in todo}
        if post_id not in found:
            log.warning("post %d (topic %d #%d): not on the topic page", post_id, topic_id, index)
            del todo[post_id]
            continue
        with db.transaction():
            for post in found.values():
                db.update_many("posts", [{"id": post.id, "source": html_to_bbcode(post.content)}])
                db.set_state(f"{ORIGIN_PREFIX}{post.id}", "html")
                db.delete_state(f"{GAP_PREFIX}{post.id}")
        for found_id in found:
            log.info("post %d: source rebuilt from the website", found_id)
            del todo[found_id]
        recovered += len(found)
    log.info("recover sources: %d recovered", recovered)
    return recovered
