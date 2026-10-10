"""What the dump says about topics -> forumer_topics flags, and their polls -> forumer_polls.

- has_poll, and the poll's results. Forum listings mark a topic with a poll
  ("Poll:" before its title, a poll icon), so a poll is known even when its
  topic page wasn't saved. Topic pages show the results. A topic saved several
  times (sessions, snapshots) shows different tallies: the one with the most
  votes, the latest, is kept.
- alert and question: the topic's icon (its first post's) was forumer's "!"
  or "?" (parse.ALERT_ICON, QUESTION_ICON), shown in listings by the title and
  on topic pages by the first post's date. Tapatalk didn't keep them.

A flag is True once any page shows it, False when a page shows the topic
without it (a listing; for the icons, also its first page), NULL when no page
says. Only forum and topic pages are read. Each one read is recorded in
crawl_state (forumer-flags:<file>), so a run picks up where the last one
stopped; the kept tallies are compared with the stored ones, so the order
doesn't matter.
"""

import json
import logging
from collections import Counter
from pathlib import Path

from tapascrape.boards.metanet import parse
from tapascrape.boards.metanet.dump import DumpFile, is_error, iter_dump
from tapascrape.boards.metanet.schema import TABLES
from tapascrape.crawl.progress import Progress
from tapascrape.db.base import Database

log = logging.getLogger(__name__)

STATE_PREFIX = "forumer-flags:"  # + dump file name: "done"
OLD_PREFIX = "forumer-polls:"    # read by older versions, for polls only: read again
BATCH = 200  # files per transaction
FLAGS = ("has_poll", "alert", "question")
ICON_FLAGS = {parse.ALERT_ICON: "alert", parse.QUESTION_ICON: "question"}
ICON_MARKS = tuple(f"icon{icon}.gif" for icon in ICON_FLAGS)


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
        self.flagged: dict[str, set[int]] = {flag: set() for flag in FLAGS}  # shown with it
        self.seen: dict[str, set[int]] = {flag: set() for flag in FLAGS}     # shown, with it or without
        self.polls: dict[int, dict] = {}
        self.files: list[str] = []

    def show(self, topic_id: int, flags, on: set) -> None:
        """A page showed the topic: with the flags in `on`, without the rest of `flags`."""
        for flag in flags:
            self.seen[flag].add(topic_id)
            if flag in on:
                self.flagged[flag].add(topic_id)


def import_topic_flags(db: Database, root: Path, refresh: bool = False) -> Counter:
    """Read the topic flags and polls of the dump at `root`; returns what was found."""
    db.create_schema(TABLES)
    db.execute(f"DELETE FROM crawl_state WHERE {db.quote_ident('key')} LIKE ?", (OLD_PREFIX + "%",))
    report: Counter = Counter()
    done = set() if refresh else set(db.states(STATE_PREFIX))
    files = [f for f in iter_dump(root) if f.kind in ("forum", "topic") and f.name not in done]
    report["files already read"] = len(done)
    known = {topic_id for topic_id, in db.query("SELECT id FROM topics")}
    stored = {topic_id for topic_id, in db.query("SELECT id FROM forumer_topics")}
    votes = dict(db.query("SELECT topic_id, vote_count FROM forumer_polls"))
    progress = Progress("forumer topic flags", len(files))
    batch = _Batch()
    for file in files:
        page = file.read()
        if not is_error(page):
            report[f"files: {file.kind}"] += 1
            if file.kind == "forum":
                for row in parse.forum_topics(page):
                    on = {ICON_FLAGS.get(row["icon"]), "has_poll" if row["poll"] else None}
                    batch.show(row["topic_id"], FLAGS, on)
            else:
                _read_topic(file, page, batch, votes)
        batch.files.append(file.name)
        progress.advance()
        if len(batch.files) >= BATCH:
            _store(db, batch, known, stored)
            batch = _Batch()
    _store(db, batch, known, stored)

    for flag in FLAGS:
        (count,), = db.query(f"SELECT COUNT(*) FROM forumer_topics WHERE {flag} = ?", (True,))
        report[f"topics flagged: {flag}"] = count
    (lost,), = db.query("SELECT COUNT(*) FROM forumer_topics WHERE has_poll = ? AND id NOT IN "
                        "(SELECT topic_id FROM forumer_polls)", (True,))
    (missing,), = db.query("SELECT COUNT(*) FROM forumer_polls p JOIN forumer_topics t ON t.id = p.topic_id "
                           "WHERE t.in_tapatalk = ?", (False,))
    report.update({"polls with results": report["topics flagged: has_poll"] - lost,
                   "polls without results (topic page not saved)": lost,
                   "polls of topics missing from Tapatalk": missing})
    return report


def _read_topic(file: DumpFile, page: str, batch: _Batch, votes: dict[int, int]) -> None:
    has_posts = parse.MSG_START.search(page) is not None  # else it's some other page a redirect landed on
    topic_id = parse.topic_of(page)[1]
    if topic_id is None and has_posts:
        topic_id = _int(file.query.get("showtopic") or file.query.get("t"))
    if topic_id is None:
        return
    if has_posts and (file.query.get("st") or "0") == "0":  # the first page: the topic's icon is its first post's
        icon = parse.first_icon(page) if any(mark in page for mark in ICON_MARKS) else None
        batch.show(topic_id, ICON_FLAGS.values(), {ICON_FLAGS.get(icon)})
    if parse.POLL_START in page:  # most topic pages have none: not worth parsing
        if poll := parse.poll(page):
            batch.show(topic_id, ["has_poll"], {"has_poll"})
            row = poll_row(topic_id, poll, file.name)
            if row["option_count"] and row["vote_count"] > votes.get(topic_id, -1):
                votes[topic_id] = row["vote_count"]
                batch.polls[topic_id] = row


def _store(db: Database, batch: _Batch, known: set[int], stored: set[int]) -> None:
    with db.transaction():
        # A flag shown anywhere wins over the pages that show the topic without it.
        for flag in FLAGS:
            db.upsert_many("forumer_topics", [{"id": topic_id, flag: True, "in_tapatalk": topic_id in known}
                                              for topic_id in sorted(batch.flagged[flag])])
            stored |= batch.flagged[flag]
        for flag in FLAGS:
            unflagged = sorted(batch.seen[flag] - batch.flagged[flag])
            for start in range(0, len(unflagged), 500):
                ids = unflagged[start:start + 500]
                db.execute(f"UPDATE forumer_topics SET {flag} = ? WHERE {flag} IS NULL AND id IN "
                           f"({', '.join('?' for _ in ids)})", [False, *ids])
            db.upsert_many("forumer_topics", [{"id": topic_id, flag: False, "in_tapatalk": topic_id in known}
                                              for topic_id in unflagged if topic_id not in stored])
            stored.update(unflagged)
        db.upsert_many("forumer_polls", list(batch.polls.values()))
        for name in batch.files:
            db.set_state(f"{STATE_PREFIX}{name}", "done")


def _int(value: str | None) -> int | None:
    return int(value) if value and value.isdigit() else None
