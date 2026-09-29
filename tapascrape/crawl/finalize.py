"""Denormalised aggregates computed from the scraped rows."""

import logging

from tapascrape.db.base import Database

log = logging.getLogger(__name__)


def finalize(db: Database) -> None:
    q = db.quote_ident
    ts, index = q("timestamp"), q("index")

    # Topics: only those whose posts were scraped; others keep the listing's counts.
    db.execute(f"""
        UPDATE topics SET
            post_count = (SELECT COUNT(*) FROM posts p WHERE p.topic_id = topics.id),
            last_post_id = (SELECT p.id FROM posts p WHERE p.topic_id = topics.id
                            ORDER BY p.{ts} DESC, p.id DESC LIMIT 1),
            created_at = (SELECT p.{ts} FROM posts p WHERE p.topic_id = topics.id
                          ORDER BY p.{index}, p.id LIMIT 1)
        WHERE EXISTS (SELECT 1 FROM posts p WHERE p.topic_id = topics.id)""")

    # Forums: over their own topics (subforums have their own rows).
    db.execute(f"""
        UPDATE forums SET
            post_count = (SELECT COALESCE(SUM(t.post_count), 0) FROM topics t
                          WHERE t.forum_id = forums.id),
            view_count = (SELECT COALESCE(SUM(t.view_count), 0) FROM topics t
                          WHERE t.forum_id = forums.id),
            last_post_id = (SELECT p.id FROM topics t JOIN posts p ON p.id = t.last_post_id
                            WHERE t.forum_id = forums.id
                            ORDER BY p.{ts} DESC, p.id DESC LIMIT 1)""")
    db.commit()
    log.info("finalize: topic and forum aggregates updated")
