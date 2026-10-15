"""What the database (and the site built from it) has: `tapascrape stats`.

Counts of forums, topics, posts, users, polls, attachments, smileys, embedded
images and quotes, broken down: how complete the crawl is, how posts got their
BBCode, what was recovered, by year, by forum. Only counts are reported: no
names or profile data, but the names of forums and groups.
"""

from collections import Counter, defaultdict

from tapascrape.content.attachments import MISSING_PREFIX as ATTACHMENT_MISSING
from tapascrape.content.images import SCOPE_USES
from tapascrape.content.smilies import MISSING_PREFIX as SMILEY_MISSING
from tapascrape.crawl.polls import STATE_PREFIX as POLL_STATE
from tapascrape.crawl.posts import GAP_PREFIX as POST_GAP
from tapascrape.crawl.sources import GAP_PREFIX as SOURCE_GAP
from tapascrape.crawl.sources import ORIGIN_PREFIX
from tapascrape.crawl.users import ORIGINAL_PREFIX as AVATAR_ORIGINAL
from tapascrape.crawl.users import STATE_PREFIX as USER_STATE
from tapascrape.db.base import Database
from tapascrape.tables import Table

SIZES = ((1, 1), (2, 10), (11, 100), (101, 1000), (1001, None))  # posts per topic or user


def sized(table: Table, counts, depth: int) -> None:
    """Rows of how many have 1, 2-10... posts (counts: one number of posts each)."""
    for low, high in SIZES:
        label = (f"{low:,} post" if low == high else f"{low:,}-{high:,} posts" if high
                 else f"over {low - 1:,} posts")
        table.add("  " * depth + label, sum(low <= n and (high is None or n <= high) for n in counts))


def year(value) -> str:
    return str(value)[:4] if value is not None else "unknown"


def embedded_images(db: Database, note: str) -> Table:
    columns = list(SCOPE_USES.values())
    rows = db.query(f"SELECT status, uses, {', '.join(columns)} FROM embedded_images")
    table = Table("Embedded images", ["Embedded in", "Images", "Uses", "Kept from the original host",
                                      "Kept from the Wayback Machine", "Missing", "Not looked for yet"],
                  note=note)
    groups = defaultdict(list)
    for status, uses, *counts in rows:
        where = " and ".join(scope for scope, n in zip(SCOPE_USES, counts) if n) or "nothing any more"
        groups[where].append((status, uses))
    for where in sorted(groups):
        items = groups[where]
        table.add(where, len(items), sum(uses or 0 for _, uses in items),
                  sum(s == "live" for s, _ in items), sum(s == "archived" for s, _ in items),
                  sum(s == "missing" for s, _ in items), sum(s is None for s, _ in items))
    return table


def quotes(db: Database, note: str) -> Table:
    table = Table("Quotes", ["", "Quotes"], note=note)
    rows = db.query("SELECT level, quoted_post_id IS NOT NULL, match FROM quotes")
    table.add("All", len(rows))
    table.add("  in the post itself", sum(level == 1 for level, _, _ in rows))
    table.add("  nested in another quote", sum(level > 1 for level, _, _ in rows))
    table.add("Linked to the post they quote", sum(bool(linked) for _, linked, _ in rows))
    for match, count in sorted(Counter(match for _, linked, match in rows if linked).items(),
                               key=lambda kv: -kv[1]):
        table.add(f"  by {match or 'unknown'}", count)
    table.add("Not linked: several posts fit as well", sum(not linked and match == "ambiguous"
                                                          for _, linked, match in rows))
    table.add("Not linked: no post fits", sum(not linked and match != "ambiguous" for _, linked, match in rows))
    return table


class Stats:
    def __init__(self, db: Database):
        self.db = db
        self.q = db.query
        self.tables: list[Table] = []
        self.overview = Table("Overview", ["", "Count"])
        self.tables.append(self.overview)

    def ids(self, sql: str, params=()) -> set:
        return {row[0] for row in self.q(sql, params)}

    def count(self, sql: str, params=()) -> int:
        return self.q(sql, params)[0][0] or 0

    def run(self) -> list[Table]:
        self.forums()
        self.topics()
        self.posts()
        self.users()
        self.by_year()
        self.polls()
        self.attachments()
        self.smileys()
        self.tables.append(embedded_images(self.db, "Images embedded with [img] from other hosts "
                                                    "(see `images`)."))
        self.tables.append(quotes(self.db, "Quotes in posts, and the posts they quote (see `quotes`)."))
        self.overview.add("Quotes", self.tables[-1].rows[0][1])
        self.files()
        return self.tables

    # -- forums and topics -------------------------------------------------------------------

    def forums(self) -> None:
        rows = self.q("SELECT id, parent_id, name, description FROM forums")
        self.overview.add("Forums", len(rows))
        topics = Counter(forum for forum, in self.q("SELECT forum_id FROM topics"))
        posts = Counter(dict(self.q("SELECT t.forum_id, COUNT(*) FROM posts p JOIN topics t "
                                    "ON t.id = p.topic_id GROUP BY t.forum_id")))
        ids = {row[0] for row in rows}
        table = Table("Forums", ["", "Forums"])
        table.add("All", len(rows))
        table.add("  at the top of the tree", sum(parent not in ids for _, parent, _, _ in rows))
        table.add("  in another forum", sum(parent in ids for _, parent, _, _ in rows))
        table.add("With topics", sum(bool(topics[forum_id]) for forum_id in ids))
        table.add("With a description", sum(bool((description or "").strip()) for *_, description in rows))
        self.tables.append(table)

        # the tree, in the order the forums are stored (the order the crawl listed them)
        children = defaultdict(list)
        for forum_id, parent, name, _ in rows:
            children[parent if parent in ids else None].append((forum_id, name))
        table = Table("Topics and posts by forum", ["Forum", "Id", "Topics", "Posts"],
                      note="A forum's own topics and posts; its subforums' are theirs.")

        def add(parent, depth):
            for forum_id, name in children[parent]:
                table.add("  " * depth + (name or f"Forum {forum_id}").strip(), forum_id, topics[forum_id],
                          posts[forum_id])
                add(forum_id, depth + 1)

        add(None, 0)
        self.tables.append(table)

    def topics(self) -> None:
        rows = self.q("SELECT id, post_count, stickied, locked FROM topics")
        stored = dict(self.q("SELECT topic_id, COUNT(*) FROM posts GROUP BY topic_id"))
        self.overview.add("Topics", len(rows))
        table = Table("Topics", ["", "Topics"], note=(
            "\"As many posts as Tapatalk counts\": every post of the topic was fetched."))
        table.add("All", len(rows))
        table.add("  with as many posts as Tapatalk counts", sum(
            count is not None and stored.get(topic_id, 0) >= count for topic_id, count, _, _ in rows))
        table.add("  with fewer", sum(count is not None and 0 < stored.get(topic_id, 0) < count
                                      for topic_id, count, _, _ in rows))
        table.add("  with no posts fetched", sum(topic_id not in stored for topic_id, *_ in rows))
        table.add("Pinned", sum(bool(pinned) for _, _, pinned, _ in rows))
        table.add("Locked", sum(bool(locked) for *_, locked in rows))
        table.add("By posts fetched", None)
        sized(table, [stored[topic_id] for topic_id, *_ in rows if topic_id in stored], 1)
        self.tables.append(table)

    # -- posts and users ---------------------------------------------------------------------

    def posts(self) -> None:
        total, guests, sources, fixed = self.q(
            "SELECT COUNT(*), SUM(user_id = 0), COUNT(source), "
            "SUM(source_fixed IS NOT NULL AND source IS NOT NULL AND source_fixed <> source) FROM posts")[0]
        self.overview.add("Posts", total)
        table = Table("Posts", ["", "Posts"], note=(
            "Posts are shown from their BBCode, fixed by `fix` where it changed something; posts with none "
            "from the HTML Tapatalk serves."))
        table.add("All", total)
        table.add("  by members", total - (guests or 0))
        table.add("  by guests", guests or 0)
        table.add("With their BBCode source", sources)
        table.add("  rebuilt from the website's HTML", len(self.db.states(ORIGIN_PREFIX)))
        table.add("  repaired by `fix`", fixed or 0)
        table.add("Without it: Tapatalk wouldn't give it", len(self.db.states(SOURCE_GAP)))
        table.add("Without it: not fetched yet", max(0, total - sources - len(self.db.states(SOURCE_GAP))))
        table.add("Known, but Tapatalk failed to serve them", len(self.db.states(POST_GAP)))
        self.tables.append(table)

    def users(self) -> None:
        rows = self.q("SELECT id, name, COALESCE(NULLIF(signature_fixed, ''), NULLIF(signature_source, ''), "
                      "NULLIF(signature, '')) IS NOT NULL, avatar_id FROM users WHERE id <> 0")
        posted = dict(self.q("SELECT user_id, COUNT(*) FROM posts WHERE user_id <> 0 GROUP BY user_id"))
        ids = {row[0] for row in rows}
        self.overview.add("Users", len(rows))
        table = Table("Users", ["", "Users"], note="Guests aren't users: their posts are counted under Posts.")
        table.add("All", len(rows))
        table.add("  with posts", sum(user_id in posted for user_id in ids))
        sized(table, [posted[user_id] for user_id in ids if user_id in posted], 2)
        table.add("  without posts", sum(user_id not in posted for user_id in ids))
        table.add("With a signature", sum(bool(signature) for *_, signature, _ in rows))
        avatars = sum(avatar_id is not None for *_, avatar_id in rows)
        table.add("With an avatar", avatars)
        table.add("  re-downloaded as the original", len(self.db.states(AVATAR_ORIGINAL)))
        table.add("Named only by their id", sum(name == str(user_id) for user_id, name, *_ in rows))
        table.add("Whose profile is gone", sum(value == "missing" for value in self.db.states(USER_STATE).values()))
        unknown = {user_id for user_id in posted if user_id not in ids}
        table.add("Posters the users table lacks", len(unknown))
        self.overview.add("Users with an avatar", avatars)
        self.tables.append(table)

        groups = self.q("SELECT g.id, g.name, COUNT(gu.user_id) FROM groups g "
                        "LEFT JOIN group_users gu ON gu.group_id = g.id GROUP BY g.id, g.name ORDER BY g.id")
        self.overview.add("Groups", len(groups))
        table = Table("Groups", ["Group", "Id", "Members"])
        for group_id, name, members in groups:
            table.add(name or f"Group {group_id}", group_id, members)
        self.tables.append(table)

    def by_year(self) -> None:
        topics = Counter(year(at) for at, in self.q("SELECT created_at FROM topics"))
        posts, posters = Counter(), defaultdict(set)
        for at, user_id in self.q("SELECT timestamp, user_id FROM posts"):
            posts[year(at)] += 1
            if user_id:
                posters[year(at)].add(user_id)
        joined = Counter(year(at) for at, in self.q("SELECT joined_at FROM users WHERE id <> 0"))
        table = Table("By year", ["Year", "Topics started", "Posts", "Members posting", "Members joining"],
                      note="Topics by the date of their first post, members by their join date.")
        for when in sorted(topics.keys() | posts.keys() | joined.keys(), key=lambda y: (y == "unknown", y)):
            table.add(when, topics[when], posts[when], len(posters[when]), joined[when])
        self.tables.append(table)

    # -- polls, attachments, smileys -----------------------------------------------------------

    def polls(self) -> None:
        results = dict(self.q("SELECT topic_id, vote_count FROM polls"))
        multiple = self.ids("SELECT topic_id FROM polls WHERE max_options > 1")
        flagged = self.ids("SELECT id FROM topics WHERE has_poll")
        missing = {int(key) for key in self.db.states(POLL_STATE)}
        self.overview.add("Polls", len(results))
        table = Table("Polls", ["", "Topics", "Votes"])
        table.add("With the poll's results", len(results), sum(votes or 0 for votes in results.values()))
        table.add("  members could pick several options", len(multiple),
                  sum(results[topic_id] or 0 for topic_id in multiple))
        table.add("Listed with a poll, but showing none", len(flagged & missing - results.keys()), None)
        table.add("Listed with a poll, results not fetched yet", len(flagged - missing - results.keys()), None)
        self.tables.append(table)

    def attachments(self) -> None:
        # the files posts and signatures use (boards may list others, with no uses)
        rows = self.q("SELECT id, kind, uses, first_post_id IS NULL, data IS NOT NULL FROM attachments "
                      "WHERE COALESCE(uses, 1) > 0")
        missing = {int(key) for key in self.db.states(ATTACHMENT_MISSING)}
        self.overview.add("Attachments", len(rows))
        table = Table("Attachments", ["", "Files", "Uses", "Kept", "Lost", "Not looked for yet"], note=(
            "Files of the old board that posts and signatures link or embed, recovered from the Wayback "
            "Machine by `attachments`."))

        def add(label, of):
            kept = sum(bool(has) for *_, has in of)
            lost = sum(not has and attachment_id in missing for attachment_id, *_, has in of)
            table.add(label, len(of), sum(uses or 0 for _, _, uses, _, _ in of), kept, lost, len(of) - kept - lost)

        add("All", rows)
        for kind, label in (("upload", "  uploads"), ("attach-id", "  downloads by id (act=Attach)")):
            add(label, [row for row in rows if row[1] == kind])
        add("Used only in signatures", [row for row in rows if row[3]])
        self.tables.append(table)

    def smileys(self) -> None:
        rows = self.q("SELECT id, host, uses, data IS NOT NULL FROM smilies")
        missing = {int(key) for key in self.db.states(SMILEY_MISSING)}
        self.overview.add("Smileys", len(rows))
        table = Table("Smileys", ["Host", "Smileys", "Uses", "Image kept", "Lost", "Not looked for yet"], note=(
            "Smileys of the posts and signatures, embedded as images from the hosts the board used (see "
            "`smilies`)."))
        for host in sorted({host or "" for _, host, _, _ in rows}):
            of = [row for row in rows if (row[1] or "") == host]
            kept = sum(bool(has) for *_, has in of)
            lost = sum(not has and smiley_id in missing for smiley_id, *_, has in of)
            table.add(host or "unknown", len(of), sum(uses or 0 for _, _, uses, _ in of), kept, lost,
                      len(of) - kept - lost)
        self.tables.append(table)
        self.overview.add("Embedded images", self.count("SELECT COUNT(*) FROM embedded_images"))

    def files(self) -> None:
        table = Table("Files kept in the database", ["", "Files", "MB"])
        for label, sql in (
                ("Avatars", "SELECT COUNT(*), SUM(LENGTH(data)) FROM avatars"),
                ("Smileys", "SELECT COUNT(data), SUM(LENGTH(data)) FROM smilies"),
                ("Attachments", "SELECT COUNT(data), SUM(LENGTH(data)) FROM attachments"),
                ("Embedded images", "SELECT COUNT(data), SUM(LENGTH(data)) FROM embedded_images")):
            count, size = self.q(sql)[0]
            table.add(label, count, f"{(size or 0) / 1048576:,.1f}")
        self.tables.append(table)


def board_stats(db: Database) -> list[Table]:
    return Stats(db).run()
