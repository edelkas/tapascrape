"""Read the Forumer dump into the forumer_* tables (idempotent: every run upserts)."""

import logging
from collections import Counter
from datetime import datetime
from pathlib import Path

from tapascrape.boards.metanet import parse
from tapascrape.boards.metanet.dump import DumpFile, archive_position, is_error, iter_dumps, newer, read_ahead
from tapascrape.boards.metanet.schema import TABLES
from tapascrape.content.attachments import looks_like_file
from tapascrape.crawl.progress import Progress
from tapascrape.db.base import Database

log = logging.getLogger(__name__)

PAGE_KINDS = {"topic", "other", "print", "forum", "profile", "members", "emoticons"}
AUTHOR_FIELDS = ("avatar_url", "title", "group", "post_count", "joined_at", "country")
PROFILE_COLUMNS = ("birthday", "location", "specific_location", "interests", "website", "msn", "aim",
                   "yahoo", "icq", "integrity", "title", "avatar_url", "post_count", "joined_at")


class Collected:
    """Everything read from the dumps, merged across the pages that repeat it: every capture
    of a page (several snapshots, sessions...) adds what it shows. Pages come oldest capture
    first, so the latest copy of a value wins (but see dump.newer)."""

    def __init__(self):
        self.posts: dict[int, tuple[parse.Post, DumpFile]] = {}  # old id -> (post, the copy kept)
        self.edited: set[int] = set()  # posts a later copy showed edited (stamped)
        self.silently_edited: set[int] = set()  # ... changed, with the same stamp
        self.members: dict[int, dict] = {}
        self.member_seen: dict[int, tuple] = {}  # when each member's kept author block was shown
        self.topics: dict[int, dict] = {}
        self.attachments: dict[tuple[int, str], dict] = {}
        self.archive: dict[tuple[int, int], dict] = {}
        self.emoticons: dict[str, str] = {}
        self.forums: dict[int, dict] = {}
        self.listed: set[int] | None = None  # the members in the latest member index read

    def add_forums(self, page: str, file: DumpFile | None = None) -> None:
        """Forums named by a page's navigation strip (with their parents) and forum listings."""
        category, chain = parse.nav_forums(page)
        for i, (forum_id, name) in enumerate(chain):
            forum = self.forums.setdefault(forum_id, {"id": forum_id})
            forum["name"] = newer(forum.get("name"), name, file)
            forum["parent_id"] = chain[i - 1][0] if i else None
            if not i and category:
                forum["category"] = newer(forum.get("category"), category, file)
        for forum_id, name, description in parse.forum_rows(page):
            forum = self.forums.setdefault(forum_id, {"id": forum_id})
            forum["name"] = newer(forum.get("name"), name, file)
            forum["description"] = newer(forum.get("description"), description, file)

    def member(self, old_id: int) -> dict:
        return self.members.setdefault(old_id, {"old_id": old_id})

    def topic(self, topic_id: int) -> dict:
        return self.topics.setdefault(topic_id, {"id": topic_id})

    def add_topic_page(self, page: parse.TopicPage, file: DumpFile) -> None:
        topic_id = page.topic_id or _int(file.query.get("showtopic") or file.query.get("t"))
        if topic_id is not None and page.posts:
            topic = self.topic(topic_id)
            for key in ("forum_id", "title", "description"):
                topic[key] = newer(topic.get(key), getattr(page, key), file)
        for post in page.posts:
            post.topic_id = post.topic_id or topic_id
            post.forum_id = post.forum_id or page.forum_id
            self.add_post(post, file)
            self.add_author(post, file)
            for attached in post.attachments:
                ref = str(attached.attach_id) if attached.kind == "file" else attached.url
                row = self.attachments.setdefault((post.id, ref), {"old_post_id": post.id, "ref": ref})
                row.update(kind=attached.kind, name=attached.name or row.get("name"))
                if attached.downloads is not None:
                    row["downloads"] = max(attached.downloads, row.get("downloads") or 0)

    def add_post(self, post: parse.Post, file: DumpFile) -> None:
        """A later copy of a post wins if it was edited since (forumer stamped edits). One with an
        older stamp, or none where the kept one has one, shows the post before an edit. With the
        same stamp, it wins too (moderators edited without a stamp), unless it may differ by
        the page, not the post: a filtered copy (dump.FILTERED_SINCE: the filter also dropped
        some links), what a lossy copy lost. A lossless copy replaces a lossy one. Where the
        post is (topic and forum: topics were moved) is the latest copy's."""
        kept = self.posts.get(post.id)
        if kept is not None:
            old, copy = kept
            if post.edited_at is not None and (old.edited_at is None or post.edited_at > old.edited_at):
                self.edited.add(post.id)
            elif post.edited_at != old.edited_at or post.html == old.html or file.filtered or not (
                    (copy.lossy and not file.lossy) or newer(old.html, post.html, file) is post.html):
                old.topic_id = post.topic_id or old.topic_id
                old.forum_id = post.forum_id or old.forum_id
                return
            elif not copy.lossy:
                self.silently_edited.add(post.id)
        self.posts[post.id] = (post, file)

    def add_author(self, post: parse.Post, file: DumpFile) -> None:
        """An author block shows the member as they were when the page was captured: the latest
        capture's wins (the latest post's, among pages of unknown date)."""
        author = post.author
        if author.member_id is None:
            return
        member = self.member(author.member_id)
        member.setdefault("name", author.name)
        seen = (file.captured_at or datetime.min, post.posted_at or datetime.min)
        if seen >= self.member_seen.get(author.member_id, (datetime.min,)):
            self.member_seen[author.member_id] = seen
            for key in AUTHOR_FIELDS:
                column = "group_name" if key == "group" else key
                member[column] = newer(member.get(column), getattr(author, key), file)
            member["signature"] = newer(member.get("signature"), post.signature, file)

    def add_profile(self, found: dict, file: DumpFile | None = None) -> None:
        member = self.member(found["member_id"])
        member.setdefault("name", found["name"])
        for key in PROFILE_COLUMNS:
            member[key] = newer(member.get(key), found.get(key), file)
        member["group_name"] = newer(member.get("group_name"), found.get("group"), file)
        if found.get("signature") and not member.get("signature"):
            member["signature"] = found["signature"]


def _int(value: str | None) -> int | None:
    return int(value) if value and value.isdigit() else None


def collect(roots: list[Path], report: Counter) -> Collected:
    found = Collected()
    restorers = set()
    log.info("forumer dump: listing the dumps' files")
    files = [file for file in iter_dumps(roots, report)
             if file.kind in PAGE_KINDS | {"attach", "archive-members", "archive-topic"}]
    progress = Progress("forumer dump", len(files))
    for file, data in read_ahead(files):
        progress.advance(detail=f"{len(found.posts):,} posts")
        if data is None:
            report["skipped: unreadable files"] += 1
            continue
        if file.restorer is not None:
            restorers.add(file.restorer)
        report[f"files: {file.kind}"] += 1
        if file.kind == "attach":
            _add_attach_file(found, file, data, report)
            continue
        if file.kind == "archive-members":
            if listed := parse.archive_members(file.text(data)):  # (the latest read wins)
                found.listed = {old_id for old_id, _ in listed}
                for old_id, name in listed:
                    found.member(old_id)["name"] = name  # the authoritative list
            continue
        if file.kind == "archive-topic":
            topic_id, offset = archive_position(file.name)
            for i, (author, day, body) in enumerate(parse.archive_posts(file.text(data))):
                kept = found.archive.get((topic_id, offset + i), {})
                found.archive[(topic_id, offset + i)] = {
                    "topic_id": topic_id, "position": offset + i, "posted_on": newer(kept.get("posted_on"), day),
                    "author": newer(kept.get("author"), author, file),
                    "html": newer(kept.get("html"), body, file)}
            continue
        page = file.text(data)
        if is_error(page):
            report["skipped: error, login or parked pages"] += 1
            continue
        for code, url in parse.emoticons(page):
            found.emoticons.setdefault(url, code)
        found.add_forums(page, file)
        if file.kind == "profile":
            if (profile := parse.profile(page)) is not None:
                found.add_profile(profile, file)
        elif file.kind == "members":
            for row in parse.member_list(page):
                member = found.member(row["member_id"])
                member.setdefault("name", row["name"])
                for key in ("joined_at", "post_count"):
                    member[key] = newer(member.get(key), row[key], file)
                member["group_name"] = newer(member.get("group_name"), row["group"], file)
        elif file.kind == "forum":
            for row in parse.forum_topics(page):
                topic = found.topic(row["topic_id"])
                for key in ("title", "description", "started_at"):
                    topic[key] = newer(topic.get(key), row[key], file)
                if row["moved"]:  # the stub a moved topic left in its old forum
                    continue
                topic["pinned"] = row["pinned"]  # as the latest listing shows it
                forum_id = _int(file.query.get("showforum") or file.query.get("f"))
                topic["forum_id"] = newer(topic.get("forum_id"), forum_id)
        else:  # topic pages, and the topic views some redirects landed on
            found.add_topic_page(parse.topic_page(page), file)
    for restorer in restorers:
        report.update(restorer.counts)
    return found


def _add_attach_file(found: Collected, file: DumpFile, data: bytes, report: Counter) -> None:
    attach_id = _int(file.query.get("id"))
    if attach_id is None or not looks_like_file(data, None):
        report["skipped: attachment downloads that aren't files"] += 1
        return
    # forumer's attachment id is the id of the post it's attached to
    row = found.attachments.setdefault((attach_id, str(attach_id)), {"old_post_id": attach_id,
                                                                     "ref": str(attach_id)})
    row.update(kind="file", data=data, source_file=file.name)


def import_dump(db: Database, roots: list[Path]) -> Counter:
    """Parse the dumps at `roots` into the forumer_* tables; returns what was found."""
    db.create_schema(TABLES)
    report: Counter = Counter()
    found = collect(roots, report)
    known_topics = {topic_id for topic_id, in db.query("SELECT id FROM topics")}
    for topic_id, (post, _) in found.posts.items():
        if post.topic_id is not None:
            found.topic(post.topic_id)
    for key, row in found.archive.items():
        found.topic(key[0])
    posts = [{"id": post.id, "topic_id": post.topic_id, "forum_id": post.forum_id,
              "member_id": post.author.member_id, "author": post.author.name, "posted": post.posted,
              "posted_at": post.posted_at, "html": post.html, "edited_by": post.edited_by,
              "edited_at": post.edited_at, "source_file": file.name}
             for post, file in found.posts.values()]
    topics = [{"id": t["id"], "forum_id": t.get("forum_id"), "title": t.get("title"),
               "description": t.get("description"), "started_at": t.get("started_at"),
               "pinned": bool(t.get("pinned")),
               "in_tapatalk": t["id"] in known_topics} for t in found.topics.values()]
    known_forums = {forum_id for forum_id, in db.query("SELECT id FROM forums")}
    forums = [{"id": f["id"], "name": f.get("name"), "description": f.get("description"),
               "parent_id": f.get("parent_id"), "category": f.get("category"),
               "in_tapatalk": f["id"] in known_forums} for f in found.forums.values()]
    member_columns = [c.name for c in TABLES[0].columns if c.name not in ("user_id", "match")]
    for old_id, member in found.members.items():
        member["listed"] = None if found.listed is None else old_id in found.listed
    members = [{c: m.get(c) for c in member_columns} for m in found.members.values()]
    attachment_columns = ("old_post_id", "ref", "kind", "name", "downloads", "data", "source_file")
    attachments = [{c: a.get(c) for c in attachment_columns} for a in found.attachments.values()]
    log.info("forumer dump: writing %d posts, %d members, %d topics", len(posts), len(members), len(topics))
    with db.transaction():
        # Batches keep each upsert's column set uniform (upsert_many writes the first row's).
        db.upsert_many("forumer_members", members)
        db.upsert_many("forumer_forums", forums)
        db.upsert_many("forumer_topics", topics)
        for start in range(0, len(posts), 5000):
            db.upsert_many("forumer_posts", posts[start:start + 5000])
        db.upsert_many("forumer_attachments", attachments)
        db.upsert_many("forumer_archive_posts", list(found.archive.values()))
        db.upsert_many("forumer_emoticons", [{"url": url, "code": code}
                                             for url, code in found.emoticons.items()])
    report.update({
        "members": len(members),
        "members pruned before the latest member index": sum(1 for m in members if m["listed"] is False),
        "members with profile details": sum(1 for m in found.members.values() if m.get("birthday")
                                           or m.get("location") or m.get("interests")),
        "posts": len(posts),
        "posts by guests": sum(1 for p in posts if p["member_id"] is None),
        "posts a later capture showed edited": len(found.edited),
        "posts a later capture showed changed without an edit stamp": len(found.silently_edited - found.edited),
        "forums": len(forums),
        "forums missing from Tapatalk": sum(1 for f in forums if not f["in_tapatalk"]),
        "topics": len(topics),
        "topics with posts missing from Tapatalk": len(
            ({p["topic_id"] for p in posts} | {topic_id for topic_id, _ in found.archive}) - known_topics),
        "topic descriptions": sum(1 for t in topics if t["description"]),
        "attachments": len(attachments),
        "attachment files": sum(1 for a in attachments if a["data"]),
        "lite archive posts": len(found.archive),
        "emoticon codes": len(found.emoticons),
    })
    return report
