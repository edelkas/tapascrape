"""Map the Forumer dump's ids to the Tapatalk database's.

Members: Tapatalk gave the migrated members new ids in the old ids' order, so
names that match exactly anchor the mapping, and members between two anchors
pair up by position when both sides have the same number of them (this also
names the accounts Tapatalk only knows by their id). Members still unplaced
get the author of their linked posts, when those all agree.

Posts: topic ids survived, and forumer showed UTC times to the minute, so a
post is the one of its topic posted in the same minute; ties are broken by
author, then by order. Old post ids grow with time, so an id referenced by a
link but missing from the dump lies between two matched neighbours: when its
topic has a single post in that time window, that's it ("interpolated").

Attachments: forumer's act=Attach&id=N used the post's own id as N, so a
mapped post ties its attachment box (original name, downloads) to the
attachments row of the file its migrated source links.
"""

import bisect
import logging
import re
from collections import Counter, defaultdict

from tapascrape.boards.metanet.schema import TABLES
from tapascrape.content.attachments import MISSING_PREFIX, canonical
from tapascrape.db.base import Database
from tapascrape.net.wayback import image_type

log = logging.getLogger(__name__)

OLD_POST_REF = re.compile(r"\[ts:topic=(\d+)[^\]]*?\bold_post=(\d+)")
ATTACHMENT_BLOCK = re.compile(r"-{5,}\[url=(/attach/ma/[^\]\s]+)\]Click here to view the attachment",
                              re.IGNORECASE)


def link(db: Database) -> Counter:
    db.create_schema(TABLES)
    report: Counter = Counter()
    with db.transaction():
        report.update(link_members(db))
        report.update(link_posts(db))
        report.update(link_members_by_posts(db))
        report.update(link_archive(db))
        report.update(link_attachments(db))
    return report


# -- members -------------------------------------------------------------------------

def increasing(pairs: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """The longest subsequence of (i, j) pairs (sorted by i) whose j increase too."""
    tails: list[int] = []  # tails[k]: index of the smallest last j of an increasing run of length k+1
    tail_values: list[int] = []
    previous = [-1] * len(pairs)
    for n, (_, j) in enumerate(pairs):
        k = bisect.bisect_left(tail_values, j)
        previous[n] = tails[k - 1] if k else -1
        if k == len(tails):
            tails.append(n)
            tail_values.append(j)
        else:
            tails[k], tail_values[k] = n, j
    out, n = [], tails[-1] if tails else -1
    while n >= 0:
        out.append(pairs[n])
        n = previous[n]
    return out[::-1]


def link_members(db: Database) -> Counter:
    old = db.query("SELECT old_id, name FROM forumer_members ORDER BY old_id")
    users = db.query("SELECT id, name FROM users WHERE id > 0 ORDER BY id")
    user_pos = {user_id: n for n, (user_id, _) in enumerate(users)}
    names = Counter(name for _, name in users)
    by_name = {name: user_id for user_id, name in users if names[name] == 1}
    old_names = Counter(name for _, name in old)
    anchors = increasing([(n, user_pos[by_name[name]]) for n, (_, name) in enumerate(old)
                          if old_names[name] == 1 and name in by_name])
    matched = {old[i][0]: (users[u][0], "name") for i, u in anchors}
    for (i, u), (i2, u2) in zip(anchors, anchors[1:]):
        if 1 < i2 - i == u2 - u:
            for step in range(1, i2 - i):
                matched[old[i + step][0]] = (users[u + step][0], "position")
    db.execute("UPDATE forumer_members SET user_id = NULL, " + db.quote_ident("match") + " = NULL")
    db.update_many("forumer_members", [{"old_id": old_id, "user_id": user_id, "match": how}
                                       for old_id, (user_id, how) in matched.items()])
    numeric = {str(user_id) for user_id, name in users if name == str(user_id)}
    report = Counter({f"members matched by {how}": n
                      for how, n in Counter(how for _, how in matched.values()).items()})
    report["members unmatched"] = len(old) - len(matched)
    report["users named by their id, now named"] = sum(
        1 for user_id, _ in matched.values() if str(user_id) in numeric)
    log.info("members: %d of %d matched", len(matched), len(old))
    return report


def link_members_by_posts(db: Database) -> Counter:
    """Match the remaining members to the author all their linked posts have."""
    authors: dict[int, set[int]] = defaultdict(set)
    for member_id, user_id in db.query(
            "SELECT f.member_id, p.user_id FROM forumer_posts f JOIN posts p ON p.id = f.post_id "
            "JOIN forumer_members m ON m.old_id = f.member_id "
            "WHERE m.user_id IS NULL AND f.match <> 'interpolated'"):
        authors[member_id].add(user_id)
    claimed = {user_id for user_id, in db.query(
        "SELECT user_id FROM forumer_members WHERE user_id IS NOT NULL")}
    owners = Counter(next(iter(users)) for users in authors.values() if len(users) == 1)
    rows = [{"old_id": member_id, "user_id": user_id, "match": "posts"}
            for member_id, users in authors.items() if len(users) == 1
            for user_id in users if user_id and user_id not in claimed and owners[user_id] == 1]
    db.update_many("forumer_members", rows)
    return Counter({"members matched by posts": len(rows), "members unmatched": -len(rows)})


def member_users(db: Database) -> dict[int, int]:
    return dict(db.query("SELECT old_id, user_id FROM forumer_members WHERE user_id IS NOT NULL"))


# -- posts ------------------------------------------------------------------------------

def link_posts(db: Database) -> Counter:
    report: Counter = Counter()
    match = db.quote_ident("match")
    db.execute("DELETE FROM forumer_posts WHERE html IS NULL")  # interpolated ids, recomputed below
    db.execute(f"UPDATE forumer_posts SET post_id = NULL, {match} = NULL")
    users = member_users(db)
    by_topic: dict[int, list[tuple]] = defaultdict(list)
    for row in db.query("SELECT id, topic_id, member_id, posted_at FROM forumer_posts "
                        "WHERE topic_id IS NOT NULL AND posted_at IS NOT NULL"):
        by_topic[row[1]].append(row)
    updates = []
    for topic_id, old_posts in by_topic.items():
        ours = db.query(f"SELECT id, user_id, {db.quote_ident('index')}, timestamp FROM posts "
                        "WHERE topic_id = ?", (topic_id,))
        if not ours:
            report["posts in topics missing from Tapatalk"] += len(old_posts)
            continue
        by_minute = defaultdict(list)
        for post in sorted(ours, key=lambda p: p[2]):
            by_minute[_minute(post[3])].append(post)
        old_by_minute = defaultdict(list)
        for post in sorted(old_posts):
            old_by_minute[_minute(post[3])].append(post)
        for minute, olds in old_by_minute.items():
            for old_id, post_id, how in _pair(olds, by_minute.get(minute, []), users):
                updates.append({"id": old_id, "post_id": post_id, "match": how})
    db.update_many("forumer_posts", updates)
    report.update({f"posts matched by {how}": n for how, n in Counter(u["match"] for u in updates).items()})
    (total,), = db.query("SELECT COUNT(*) FROM forumer_posts")
    report["posts unmatched"] = total - len(updates) - report["posts in topics missing from Tapatalk"]
    report.update(interpolate_references(db))
    (guests,), = db.query("SELECT COUNT(*) FROM forumer_posts f JOIN posts p ON p.id = f.post_id "
                          "WHERE p.user_id = 0 AND f.author IS NOT NULL")
    report["guest posts given an author"] = guests
    return report


def _minute(value) -> str:
    return str(value)[:16]


def _pair(olds: list[tuple], ours: list[tuple], users: dict[int, int]) -> list[tuple[int, int, str]]:
    """Pair the dump's posts of a topic and minute with ours: (old id, post id, how)."""
    if not ours:
        return []
    if len(olds) == 1 and len(ours) == 1:
        return [(olds[0][0], ours[0][0], "time")]
    pairs, left, free = [], [], list(ours)
    for old in olds:
        user_id = users.get(old[2]) if old[2] is not None else 0
        same = [post for post in free if post[1] == user_id] if user_id is not None else []
        if len(same) == 1:
            pairs.append((old[0], same[0][0], "time+author"))
            free.remove(same[0])
        else:
            left.append(old)
    if left and len(left) == len(free):  # both in posting order
        pairs += [(old[0], post[0], "time+order") for old, post in zip(left, free)]
    return pairs


class TimeScale:
    """Old post ids grow with time: the matched ones bound when an unmatched one was posted."""

    def __init__(self, db: Database):
        self.matched = sorted(db.query("SELECT f.id, p.timestamp FROM forumer_posts f "
                                       "JOIN posts p ON p.id = f.post_id "
                                       "WHERE f.match <> 'interpolated'"))
        self.ids = [old_id for old_id, _ in self.matched]

    def window(self, old_id: int) -> tuple | None:
        """(earliest, latest) time post `old_id` can have, or None when unbounded/inconsistent."""
        k = bisect.bisect_left(self.ids, old_id)
        if k == 0 or k == len(self.ids):
            return None
        low, high = self.matched[k - 1][1], self.matched[k][1]
        return (low, high) if low <= high else None


def interpolate_references(db: Database) -> Counter:
    """Map the old post ids links refer to (old_post= in sentinels) that the dump lacks."""
    refs: dict[int, int] = {}
    for column, table in (("source_fixed", "posts"), ("signature_fixed", "users")):
        for text, in db.query(f"SELECT {column} FROM {table} WHERE {column} LIKE ?", ("%old_post=%",)):
            for topic_id, old_id in OLD_POST_REF.findall(text):
                refs[int(old_id)] = int(topic_id)
    known = dict(db.query("SELECT id, post_id FROM forumer_posts"))
    scale = TimeScale(db)
    taken = {post_id for post_id in known.values() if post_id is not None}
    rows, report = [], Counter()
    for old_id, topic_id in sorted(refs.items()):
        if known.get(old_id) is not None:
            report["linked post ids found directly"] += 1
            continue
        if (window := scale.window(old_id)) is None:
            report["linked post ids unresolved"] += 1
            continue
        low, high = window
        candidates = [post_id for post_id, in db.query(
            "SELECT id FROM posts WHERE topic_id = ? AND timestamp >= ? AND timestamp <= ?",
            (topic_id, low, high)) if post_id not in taken]
        if len(candidates) == 1:
            rows.append({"id": old_id, "topic_id": topic_id, "post_id": candidates[0],
                         "match": "interpolated"})
            taken.add(candidates[0])
        else:
            report["linked post ids unresolved"] += 1
    db.upsert_many("forumer_posts", rows)
    report["linked post ids interpolated"] = len(rows)
    return report


# -- lite archive ------------------------------------------------------------------------

def link_archive(db: Database) -> Counter:
    """Tie lite-archive posts (author + day, no ids) to ours: same topic, day and author name,
    or else the same position."""
    names = dict(db.query("SELECT id, name FROM users"))
    restored = dict(db.query("SELECT user_id, name FROM forumer_members WHERE user_id IS NOT NULL"))
    guests = dict(db.query("SELECT post_id, author FROM forumer_posts WHERE post_id IS NOT NULL"))
    db.execute("UPDATE forumer_archive_posts SET post_id = NULL")
    by_topic: dict[int, list[tuple]] = defaultdict(list)
    for row in db.query("SELECT topic_id, position, author, posted_on FROM forumer_archive_posts"):
        by_topic[row[0]].append(row)
    updates = []
    for topic_id, rows in by_topic.items():
        ours = db.query(f"SELECT id, user_id, {db.quote_ident('index')}, timestamp FROM posts "
                        "WHERE topic_id = ?", (topic_id,))
        by_day = defaultdict(list)
        for post in ours:
            by_day[str(post[3])[:10]].append(post)
        taken = set()
        for _, position, author, day in rows:
            same_day = [p for p in by_day.get(str(day)[:10], []) if p[0] not in taken]
            named = [p for p in same_day if author in (names.get(p[1]), restored.get(p[1]),
                                                       guests.get(p[0]))]
            placed = [p for p in same_day if p[2] == position + 1]
            pick = named if len(named) == 1 else placed if len(placed) == 1 else []
            if pick:
                taken.add(pick[0][0])
                updates.append({"topic_id": topic_id, "position": position, "post_id": pick[0][0]})
    db.update_many("forumer_archive_posts", updates)
    (total,), = db.query("SELECT COUNT(*) FROM forumer_archive_posts")
    return Counter({"lite archive posts matched": len(updates),
                    "lite archive posts unmatched": total - len(updates)})


# -- attachments -----------------------------------------------------------------------------

def post_upload(source: str | None, by_url: dict[str, int]) -> int | None:
    """The attachments row of the file a migrated post was posted with (its attachment block)."""
    if source and (block := ATTACHMENT_BLOCK.search(source)) and (found := canonical(block.group(1))):
        return by_url.get(found.url)
    return None


def link_attachments(db: Database) -> Counter:
    by_url = dict(db.query("SELECT url, id FROM attachments"))
    by_attach_id = defaultdict(list)
    for attachment_id, old_attach_id in db.query(
            "SELECT id, old_attach_id FROM attachments WHERE old_attach_id IS NOT NULL"):
        by_attach_id[old_attach_id].append(attachment_id)
    owners = dict(db.query("SELECT f.id, p.source FROM forumer_posts f "
                           "JOIN posts p ON p.id = f.post_id"))
    report, updates, copies = Counter(), [], []
    for old_post_id, ref, kind, name, data, source_file in db.query(
            "SELECT old_post_id, ref, kind, name, data, source_file FROM forumer_attachments"):
        attachment_id = None
        if kind == "image":
            found = canonical(ref)
            attachment_id = by_url.get(found.url) if found else None
        else:
            attachment_id = post_upload(owners.get(old_post_id), by_url)
        updates.append({"old_post_id": old_post_id, "ref": ref, "attachment_id": attachment_id,
                        "content_type": image_type(data) if data else None})
        report["attachments tied to a file of ours" if attachment_id else "attachments not tied"] += 1
        if data:
            targets = {attachment_id} - {None}
            if kind == "file" and ref.isdigit():
                targets.update(by_attach_id.get(int(ref), []))
            copies += [(target, data, name, source_file) for target in targets]
    db.update_many("forumer_attachments", updates)
    report.update(pair_attach_ids(db, by_url, owners))
    stored = 0
    for attachment_id, data, name, source_file in copies:
        (row,) = db.query("SELECT data IS NULL, name FROM attachments WHERE id = ?", (attachment_id,))
        if not row[0]:
            continue
        values = {"id": attachment_id, "data": data, "size": len(data),
                  "content_type": image_type(data), "recovered_from": f"forumer-dump:{source_file}"}
        if row[1] is None and name:
            values["name"] = name
        db.update_many("attachments", [values])
        db.delete_state(f"{MISSING_PREFIX}{attachment_id}")
        stored += 1
    report["attachment files stored from the dump"] = stored
    return report


def pair_attach_ids(db: Database, by_url: dict[str, int], owners: dict[int, str]) -> Counter:
    """Tie act=Attach&id=N rows to the upload row of post N (the same file), and copy the file
    between the two when only one has it."""
    known = {(old_post_id, ref) for old_post_id, ref in db.query(
        "SELECT old_post_id, ref FROM forumer_attachments")}
    rows, copied = [], 0
    for attach_row, old_attach_id, data, mime, source in db.query(
            "SELECT id, old_attach_id, data, content_type, recovered_from FROM attachments "
            "WHERE kind = 'attach-id' AND old_attach_id IS NOT NULL"):
        upload = post_upload(owners.get(old_attach_id), by_url)
        if upload is None:
            continue
        if (old_attach_id, str(old_attach_id)) not in known:
            rows.append({"old_post_id": old_attach_id, "ref": str(old_attach_id), "kind": "file",
                         "attachment_id": upload})
        (upload_data, upload_mime, upload_source), = db.query(
            "SELECT data, content_type, recovered_from FROM attachments WHERE id = ?", (upload,))
        if (data is None) == (upload_data is None):
            continue
        if upload_data is None:
            target, file, file_mime, origin = upload, data, mime, source
        else:
            target, file, file_mime, origin = attach_row, upload_data, upload_mime, upload_source
        db.update_many("attachments", [{"id": target, "data": file, "size": len(file),
                                        "content_type": file_mime, "recovered_from": origin}])
        db.delete_state(f"{MISSING_PREFIX}{target}")
        copied += 1
    db.upsert_many("forumer_attachments", rows)
    return Counter({"attachment files copied between act=Attach and upload rows": copied})
