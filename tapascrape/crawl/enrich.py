"""HTML pass: what the API doesn't provide.

- profiles: users.rank, users.signature, group names and memberships;
- gaps: posts the API can't return (see crawl.posts.fetch_range), recovered
  from the topic page. Their content is the website's HTML rendering, not the
  API's, so they're tagged "post-source:<id>" = "html" in crawl_state.
"""

import logging

from tapascrape.crawl.posts import GAP_PREFIX
from tapascrape.crawl.progress import Progress
from tapascrape.db.base import Database
from tapascrape.net.web import WebClient
from tapascrape.parse.profile import parse_profile
from tapascrape.parse.topic import parse_topic_posts

log = logging.getLogger(__name__)

PROFILE_PREFIX = "profile:"  # value: "done" or "missing"
SOURCE_PREFIX = "post-source:"


def pending_profiles(db: Database, refresh: bool = False) -> list[int]:
    ids = {r[0] for r in db.query("SELECT id FROM users WHERE id > 0")}
    if not refresh:
        ids -= {int(k) for k in db.states(PROFILE_PREFIX)}
    return sorted(ids)


def enrich_profiles(web: WebClient, db: Database, refresh: bool = False) -> int:
    todo = pending_profiles(db, refresh)
    if not todo:
        log.info("profiles: nothing to do")
        return 0
    progress = Progress("profiles", len(todo))
    stored = missing = 0
    for user_id in todo:
        profile = parse_profile(web.get(web.config.profile_url(user_id)))
        with db.transaction():
            if profile is None:
                db.set_state(f"{PROFILE_PREFIX}{user_id}", "missing")
                missing += 1
            else:
                db.update_many("users", [{"id": user_id, "rank": profile.rank,
                                          "signature": profile.signature}])
                db.upsert_many("groups", [{"id": g.id, "name": g.name} for g in profile.groups])
                db.upsert_many("group_users", [{"group_id": g.id, "user_id": user_id}
                                               for g in profile.groups])
                db.set_state(f"{PROFILE_PREFIX}{user_id}", "done")
                stored += 1
        progress.advance()
    log.info("profiles: %d enriched, %d missing", stored, missing)
    return stored


def recover_gaps(web: WebClient, db: Database) -> int:
    gaps = sorted(tuple(map(int, key.split(":"))) for key in db.states(GAP_PREFIX))
    if not gaps:
        log.info("gaps: nothing to do")
        return 0
    recovered = 0
    for topic_id, offset in gaps:
        html = web.get(f"viewtopic.php?t={topic_id}&start={offset}")
        post = next((p for p in parse_topic_posts(html) if p.index == offset + 1), None)
        if post is None:
            log.warning("topic %d post #%d: not found on the topic page either", topic_id, offset + 1)
            continue
        with db.transaction():
            db.upsert_many("posts", [{"id": post.id, "topic_id": topic_id, "user_id": post.user_id,
                                      "index": post.index, "timestamp": post.timestamp,
                                      "content": post.content}])
            if post.user_id and post.author_name:
                db.upsert_many("users", [{"id": post.user_id, "name": post.author_name}])
            db.set_state(f"{SOURCE_PREFIX}{post.id}", "html")
            db.delete_state(f"{GAP_PREFIX}{topic_id}:{offset}")
        log.info("topic %d post #%d: recovered post %d from HTML", topic_id, offset + 1, post.id)
        recovered += 1
    return recovered
