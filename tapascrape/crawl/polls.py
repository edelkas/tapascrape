"""Polls of the topics whose listing says they have one -> polls table.

One get_thread call per poll; topics without one (most) are never opened.
"""

import json
import logging

from tapascrape.crawl.progress import Progress
from tapascrape.db.base import Database
from tapascrape.net.api import ApiError, TapatalkApi

log = logging.getLogger(__name__)

STATE_PREFIX = "poll:"  # value: "missing" (listed with a poll, but the thread has none)


def pending_polls(db: Database, forum_ids: list[int] | None = None) -> list[int]:
    """Topics listed with a poll that isn't stored yet."""
    sql = "SELECT id FROM topics WHERE has_poll = ? AND id NOT IN (SELECT topic_id FROM polls)"
    params: list = [True]
    if forum_ids is not None:
        sql += f" AND forum_id IN ({', '.join('?' for _ in forum_ids)})"
        params += forum_ids
    missing = {int(k) for k in db.states(STATE_PREFIX)}
    return [topic_id for topic_id, in db.query(sql + " ORDER BY id", params) if topic_id not in missing]


def poll_row(topic_id: int, raw: dict) -> dict:
    options = [{"text": option.get("text", ""), "votes": int(option.get("vote_count") or 0)}
               for option in raw.get("options") or []]
    return {"topic_id": topic_id, "title": raw.get("title") or None,
            "vote_count": sum(option["votes"] for option in options), "option_count": len(options),
            "max_options": int(raw["max_options"]) if raw.get("max_options") else None,
            "options": json.dumps(options, ensure_ascii=False)}


def crawl_polls(api: TapatalkApi, db: Database, forum_ids: list[int] | None = None) -> int:
    todo = pending_polls(db, forum_ids)
    if not todo:
        log.info("polls: nothing to do")
        return 0
    progress = Progress("polls", len(todo))
    stored = failed = 0
    for topic_id in todo:
        try:
            raw = api.poll(topic_id)
        except ApiError as e:
            log.warning("topic %d: %s", topic_id, e)  # stays pending: retried next run
            failed += 1
            continue
        with db.transaction():
            if raw is None:
                log.warning("topic %d: listed with a poll, but the thread has none", topic_id)
                db.set_state(f"{STATE_PREFIX}{topic_id}", "missing")
            else:
                db.upsert_many("polls", [poll_row(topic_id, raw)])
                stored += 1
        progress.advance()
    log.info("polls: %d stored%s", stored, f", {failed} failed (rerun to retry)" if failed else "")
    return stored
