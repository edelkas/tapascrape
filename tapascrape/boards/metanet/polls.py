"""The dump's polls -> forumer_polls, and which topics have one -> forumer_topics.has_poll.

Forum listings mark a topic with a poll ("Poll:" before its title, a poll
icon), so a poll is known even when its topic page wasn't saved. Topic pages
show the results. A topic saved several times (sessions, snapshots) shows
different tallies: the one with the most votes, the latest, is kept.

Only forum and topic pages are read. Each one read is recorded in crawl_state
(forumer-polls:<file>), so a run picks up where the last one stopped; the
kept tallies are compared with the stored ones, so the order doesn't matter.
"""

import json
import logging
from collections import Counter
from pathlib import Path

from tapascrape.boards.metanet import parse
from tapascrape.boards.metanet.dump import is_error, iter_dump
from tapascrape.boards.metanet.schema import TABLES
from tapascrape.crawl.progress import Progress
from tapascrape.db.base import Database

log = logging.getLogger(__name__)

STATE_PREFIX = "forumer-polls:"  # + dump file name: "done"
BATCH = 200  # files per transaction


def poll_row(topic_id: int, poll: dict, source_file: str) -> dict:
    options = [{"text": text, "votes": votes} for text, votes in poll["options"]]
    votes = poll.get("votes")
    return {"topic_id": topic_id, "title": poll.get("question"),
            "vote_count": votes if votes is not None else sum(o["votes"] for o in options),
            "option_count": len(options),
            "max_options": None,  # forumer's pages don't say
            "options": json.dumps(options, ensure_ascii=False), "source_file": source_file}


class _Batch:
    def __init__(self):
        self.flagged: set[int] = set()  # topics with a poll
        self.listed: set[int] = set()   # topics listed without one
        self.polls: dict[int, dict] = {}
        self.files: list[str] = []


def import_polls(db: Database, root: Path, refresh: bool = False) -> Counter:
    """Read the polls of the dump at `root`; returns what was found."""
    db.create_schema(TABLES)
    report: Counter = Counter()
    done = set() if refresh else set(db.states(STATE_PREFIX))
    files = [f for f in iter_dump(root) if f.kind in ("forum", "topic") and f.name not in done]
    report["files already read"] = len(done)
    known = {topic_id for topic_id, in db.query("SELECT id FROM topics")}
    stored = {topic_id for topic_id, in db.query("SELECT id FROM forumer_topics")}
    votes = dict(db.query("SELECT topic_id, vote_count FROM forumer_polls"))
    progress = Progress("forumer polls", len(files))
    batch = _Batch()
    for file in files:
        page = file.read()
        if not is_error(page):
            report[f"files: {file.kind}"] += 1
            if file.kind == "forum":
                for row in parse.forum_topics(page):
                    (batch.flagged if row["poll"] else batch.listed).add(row["topic_id"])
            elif parse.POLL_START in page:  # most topic pages have none: not worth parsing
                topic = parse.topic_page(page)
                topic_id = topic.topic_id
                if topic_id is None and topic.posts:  # else it's some other page a redirect landed on
                    topic_id = _int(file.query.get("showtopic") or file.query.get("t"))
                if topic_id is not None and topic.poll:
                    batch.flagged.add(topic_id)
                    row = poll_row(topic_id, topic.poll, file.name)
                    if row["option_count"] and row["vote_count"] > votes.get(topic_id, -1):
                        votes[topic_id] = row["vote_count"]
                        batch.polls[topic_id] = row
        batch.files.append(file.name)
        progress.advance()
        if len(batch.files) >= BATCH:
            _store(db, batch, known, stored)
            batch = _Batch()
    _store(db, batch, known, stored)

    (flagged,), = db.query("SELECT COUNT(*) FROM forumer_topics WHERE has_poll = ?", (True,))
    (lost,), = db.query("SELECT COUNT(*) FROM forumer_topics WHERE has_poll = ? AND id NOT IN "
                        "(SELECT topic_id FROM forumer_polls)", (True,))
    (missing,), = db.query("SELECT COUNT(*) FROM forumer_polls p JOIN forumer_topics t ON t.id = p.topic_id "
                           "WHERE t.in_tapatalk = ?", (False,))
    report.update({"topics with a poll": flagged, "polls with results": flagged - lost,
                   "polls without results (topic page not saved)": lost,
                   "polls of topics missing from Tapatalk": missing})
    return report


def _store(db: Database, batch: _Batch, known: set[int], stored: set[int]) -> None:
    with db.transaction():
        # A poll seen anywhere wins over listings that don't show one.
        db.upsert_many("forumer_topics", [{"id": topic_id, "has_poll": True, "in_tapatalk": topic_id in known}
                                          for topic_id in sorted(batch.flagged)])
        stored |= batch.flagged
        unflagged = sorted(batch.listed - batch.flagged)
        for start in range(0, len(unflagged), 500):
            ids = unflagged[start:start + 500]
            db.execute(f"UPDATE forumer_topics SET has_poll = ? WHERE has_poll IS NULL AND id IN "
                       f"({', '.join('?' for _ in ids)})", [False, *ids])
        db.upsert_many("forumer_topics", [{"id": topic_id, "has_poll": False, "in_tapatalk": topic_id in known}
                                          for topic_id in unflagged if topic_id not in stored])
        stored.update(unflagged)
        db.upsert_many("forumer_polls", list(batch.polls.values()))
        for name in batch.files:
            db.set_state(f"{STATE_PREFIX}{name}", "done")


def _int(value: str | None) -> int | None:
    return int(value) if value and value.isdigit() else None
