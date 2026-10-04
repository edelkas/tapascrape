"""Read the Forumer dump into the forumer_* tables (idempotent: every run upserts)."""

import logging
from collections import Counter
from datetime import datetime
from pathlib import Path

from tapascrape.boards.metanet import parse
from tapascrape.boards.metanet.dump import DumpFile, archive_position, is_error, iter_dump
from tapascrape.boards.metanet.schema import TABLES
from tapascrape.content.attachments import looks_like_file
from tapascrape.db.base import Database

log = logging.getLogger(__name__)

PAGE_KINDS = {"topic", "other", "print", "forum", "profile", "members", "emoticons"}
AUTHOR_FIELDS = ("avatar_url", "title", "group", "post_count", "joined_at", "country")
PROFILE_COLUMNS = ("birthday", "location", "specific_location", "interests", "website", "msn", "aim",
                   "yahoo", "icq", "integrity", "title", "avatar_url", "post_count", "joined_at")


class Collected:
    """Everything read from the dump, merged across the pages that repeat it."""

    def __init__(self):
        self.posts: dict[int, tuple[parse.Post, str]] = {}  # old id -> (post, file)
        self.members: dict[int, dict] = {}
        self.member_seen: dict[int, datetime] = {}  # when each member's author block was shown
        self.topics: dict[int, dict] = {}
        self.attachments: dict[tuple[int, str], dict] = {}
        self.archive: dict[tuple[int, int], dict] = {}
        self.emoticons: dict[str, str] = {}

    def member(self, old_id: int) -> dict:
        return self.members.setdefault(old_id, {"old_id": old_id})

    def topic(self, topic_id: int) -> dict:
        return self.topics.setdefault(topic_id, {"id": topic_id})

    def add_topic_page(self, page: parse.TopicPage, file: DumpFile) -> None:
        topic_id = page.topic_id or _int(file.query.get("showtopic") or file.query.get("t"))
        if topic_id is not None and page.posts:
            topic = self.topic(topic_id)
            for key in ("forum_id", "title", "description"):
                if getattr(page, key) and not topic.get(key):
                    topic[key] = getattr(page, key)
            # Pages saved at different times show different tallies; keep the latest (most votes).
            if page.poll and (page.poll.get("votes") or 0) >= (topic.get("_votes") or -1):
                topic["poll"], topic["_votes"] = parse.poll_json(page.poll), page.poll.get("votes") or 0
        for post in page.posts:
            post.topic_id = post.topic_id or topic_id
            post.forum_id = post.forum_id or page.forum_id
            if post.id not in self.posts or self.posts[post.id][0].topic_id is None:
                self.posts[post.id] = (post, file.name)
            self.add_author(post)
            for attached in post.attachments:
                ref = str(attached.attach_id) if attached.kind == "file" else attached.url
                row = self.attachments.setdefault((post.id, ref), {"old_post_id": post.id, "ref": ref})
                row.update(kind=attached.kind, name=attached.name or row.get("name"))
                if attached.downloads is not None:
                    row["downloads"] = max(attached.downloads, row.get("downloads") or 0)

    def add_author(self, post: parse.Post) -> None:
        author = post.author
        if author.member_id is None:
            return
        member = self.member(author.member_id)
        member.setdefault("name", author.name)
        seen = post.posted_at or datetime.min
        if seen >= self.member_seen.get(author.member_id, datetime.min):  # the latest wins
            self.member_seen[author.member_id] = seen
            for key in AUTHOR_FIELDS:
                if getattr(author, key) is not None:
                    member["group_name" if key == "group" else key] = getattr(author, key)
            if post.signature:
                member["signature"] = post.signature

    def add_profile(self, found: dict) -> None:
        member = self.member(found["member_id"])
        member.setdefault("name", found["name"])
        for key in PROFILE_COLUMNS:
            if found.get(key) is not None:
                member[key] = found[key]
        if found.get("group"):
            member["group_name"] = found["group"]
        if found.get("signature") and not member.get("signature"):
            member["signature"] = found["signature"]


def _int(value: str | None) -> int | None:
    return int(value) if value and value.isdigit() else None


def collect(root: Path, report: Counter) -> Collected:
    found = Collected()
    for file in iter_dump(root):
        report[f"files: {file.kind}"] += 1
        if file.kind == "attach":
            _add_attach_file(found, file, report)
            continue
        if file.kind == "archive-members":
            for old_id, name in parse.archive_members(file.read()):
                found.member(old_id)["name"] = name  # the authoritative list
            continue
        if file.kind == "archive-topic":
            topic_id, offset = archive_position(file.name)
            for i, (author, day, body) in enumerate(parse.archive_posts(file.read())):
                found.archive[(topic_id, offset + i)] = {
                    "topic_id": topic_id, "position": offset + i, "author": author,
                    "posted_on": day, "html": body}
            continue
        if file.kind not in PAGE_KINDS:
            continue
        page = file.read()
        if is_error(page):
            report["skipped: error, login or parked pages"] += 1
            continue
        for code, url in parse.emoticons(page):
            found.emoticons.setdefault(url, code)
        if file.kind == "profile":
            if (profile := parse.profile(page)) is not None:
                found.add_profile(profile)
        elif file.kind == "members":
            for row in parse.member_list(page):
                member = found.member(row["member_id"])
                member.setdefault("name", row["name"])
                for key in ("joined_at", "post_count"):
                    member.setdefault(key, row[key])
                member.setdefault("group_name", row["group"])
        elif file.kind == "forum":
            for row in parse.forum_topics(page):
                topic = found.topic(row["topic_id"])
                for key in ("title", "description", "started_at"):
                    if row[key] and not topic.get(key):
                        topic[key] = row[key]
                topic["pinned"] = topic.get("pinned") or row["pinned"]
                if forum_id := _int(file.query.get("showforum") or file.query.get("f")):
                    topic.setdefault("forum_id", forum_id)
        else:  # topic pages, and the topic views some redirects landed on
            found.add_topic_page(parse.topic_page(page), file)
    return found


def _add_attach_file(found: Collected, file: DumpFile, report: Counter) -> None:
    attach_id = _int(file.query.get("id"))
    data = file.path.read_bytes()
    if attach_id is None or not looks_like_file(data, None):
        report["skipped: attachment downloads that aren't files"] += 1
        return
    # forumer's attachment id is the id of the post it's attached to
    row = found.attachments.setdefault((attach_id, str(attach_id)), {"old_post_id": attach_id,
                                                                     "ref": str(attach_id)})
    row.update(kind="file", data=data, source_file=file.name)


def import_dump(db: Database, root: Path) -> Counter:
    """Parse the dump at `root` into the forumer_* tables; returns what was found."""
    db.create_schema(TABLES)
    report: Counter = Counter()
    found = collect(root, report)
    known_topics = {topic_id for topic_id, in db.query("SELECT id FROM topics")}
    for topic_id, (post, _) in found.posts.items():
        if post.topic_id is not None:
            found.topic(post.topic_id)
    for key, row in found.archive.items():
        found.topic(key[0])
    posts = [{"id": post.id, "topic_id": post.topic_id, "forum_id": post.forum_id,
              "member_id": post.author.member_id, "author": post.author.name, "posted": post.posted,
              "posted_at": post.posted_at, "html": post.html, "edited_by": post.edited_by,
              "edited_at": post.edited_at, "source_file": file}
             for post, file in found.posts.values()]
    topics = [{"id": t["id"], "forum_id": t.get("forum_id"), "title": t.get("title"),
               "description": t.get("description"), "started_at": t.get("started_at"),
               "pinned": bool(t.get("pinned")), "poll": t.get("poll"),
               "in_tapatalk": t["id"] in known_topics} for t in found.topics.values()]
    member_columns = [c.name for c in TABLES[0].columns if c.name not in ("user_id", "match")]
    members = [{c: m.get(c) for c in member_columns} for m in found.members.values()]
    attachment_columns = ("old_post_id", "ref", "kind", "name", "downloads", "data", "source_file")
    attachments = [{c: a.get(c) for c in attachment_columns} for a in found.attachments.values()]
    with db.transaction():
        # Batches keep each upsert's column set uniform (upsert_many writes the first row's).
        db.upsert_many("forumer_members", members)
        db.upsert_many("forumer_topics", topics)
        for start in range(0, len(posts), 5000):
            db.upsert_many("forumer_posts", posts[start:start + 5000])
        db.upsert_many("forumer_attachments", attachments)
        db.upsert_many("forumer_archive_posts", list(found.archive.values()))
        db.upsert_many("forumer_emoticons", [{"url": url, "code": code}
                                             for url, code in found.emoticons.items()])
    report.update({
        "members": len(members),
        "members with profile details": sum(1 for m in found.members.values() if m.get("birthday")
                                           or m.get("location") or m.get("interests")),
        "posts": len(posts),
        "posts by guests": sum(1 for p in posts if p["member_id"] is None),
        "topics": len(topics),
        "topics with posts missing from Tapatalk": len(
            ({p["topic_id"] for p in posts} | {topic_id for topic_id, _ in found.archive}) - known_topics),
        "topic descriptions": sum(1 for t in topics if t["description"]),
        "polls": sum(1 for t in topics if t["poll"]),
        "attachments": len(attachments),
        "attachment files": sum(1 for a in attachments if a["data"]),
        "lite archive posts": len(found.archive),
        "emoticon codes": len(found.emoticons),
    })
    log.info("forumer dump: %d posts, %d members, %d topics", len(posts), len(members), len(topics))
    return report
