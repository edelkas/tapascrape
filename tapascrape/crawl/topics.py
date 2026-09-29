"""Topic listings -> topics table."""

import logging

from tapascrape.crawl.progress import Progress
from tapascrape.db.base import Database
from tapascrape.models import Forum, Topic
from tapascrape.net.api import TapatalkApi

log = logging.getLogger(__name__)

STATE_PREFIX = "forum-topics:"


def topic_row(t: Topic) -> dict:
    # created_at / last_post_id are left to finalize, so re-listing keeps them.
    return {"id": t.id, "forum_id": t.forum_id, "user_id": t.user_id, "name": t.name,
            "stickied": t.stickied, "locked": t.locked,
            "post_count": t.post_count, "view_count": t.view_count}


def crawl_topics(api: TapatalkApi, db: Database, forums: list[Forum], refresh: bool = False) -> int:
    """List the topics of each forum. Forums already listed are skipped unless `refresh`."""
    done = set() if refresh else set(db.states(STATE_PREFIX))
    todo = [f for f in forums if not f.is_category and str(f.id) not in done]
    progress = Progress("topic listings", len(todo), every=0)
    total = 0
    for forum in todo:
        topics = list(api.iter_topics(forum.id))
        with db.transaction():
            db.upsert_many("topics", [topic_row(t) for t in topics])
            # Seed authors so the users step knows them even if their profile is gone.
            db.upsert_many("users", seed_users((t.user_id, t.author_name) for t in topics))
            db.set_state(f"{STATE_PREFIX}{forum.id}", "done")
        total += len(topics)
        progress.advance(detail=f"[{forum.id}] {forum.name}: {len(topics)} topics")
    return total


def seed_users(pairs) -> list[dict]:
    """Minimal users rows (id, name) from author (id, name) pairs, deduplicated."""
    return list({uid: {"id": uid, "name": name} for uid, name in pairs if uid and name}.values())
