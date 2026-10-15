"""What the Metanet site shows, and where it comes from: `tapascrape metanet stats`.

The site (site.MetanetSite) merges what Tapatalk has with what the Forumer dump
adds. Each kind of thing (forums, topics, posts, members...) is counted here
as three sets of keys, under the same rules the site follows:

- Tapatalk: what the Tapatalk tables have;
- Forumer: what the forumer_* tables have (matched to Tapatalk's where `link`
  matched them, so a thing both have is counted once);
- shown: what the site shows. Mostly both together, but not always: posts of
  the dump that `link` couldn't match, in topics Tapatalk has, aren't shown,
  for instance.

Keys are Tapatalk's ids, or ("f", old id) for what only the dump has. Only
counts are reported: no names or profile data, but the names of forums.
"""

from collections import Counter, defaultdict

from tapascrape.boards.metanet.schema import TABLES
from tapascrape.content.images import SCOPE_USES
from tapascrape.content.smilies import normalize_url
from tapascrape.db.base import Database
from tapascrape.tables import Table

SOURCES = ["", "Shown", "Tapatalk", "Forumer", "Tapatalk only", "Forumer only", "Both"]
WAYBACK = "https://web.archive.org/"
DUMP = "forumer-dump:"


def sources_row(table: Table, label: str, shown: set, tapatalk: set, forumer: set) -> None:
    table.add(label, len(shown), len(tapatalk), len(forumer), len(tapatalk - forumer),
              len(forumer - tapatalk), len(tapatalk & forumer))


def filled(value) -> bool:
    return value is not None and str(value).strip() != ""


class Stats:
    def __init__(self, db: Database):
        self.db = db
        self.q = db.query
        self.tables: list[Table] = []
        self.overview = Table("Overview", SOURCES, note=(
            "Shown: on the site. Tapatalk, Forumer: what each source has; a thing both have counts once, "
            "in Both."))
        self.tables.append(self.overview)

    def ids(self, sql: str, params=()) -> set:
        return {row[0] for row in self.q(sql, params)}

    def run(self) -> list[Table]:
        self.forums()
        self.topics()
        self.posts()
        self.members()
        self.avatars()
        self.polls()
        self.attachments()
        self.smileys()
        self.images()
        self.quotes()
        self.files()
        return self.tables

    # -- forums and topics -------------------------------------------------------------------

    def forums(self) -> None:
        tapatalk = self.ids("SELECT id FROM forums")
        forumer = self.ids("SELECT id FROM forumer_forums")
        sources_row(self.overview, "Forums", tapatalk | forumer, tapatalk, forumer)
        names = dict(self.q("SELECT id, name FROM forums"))
        names.update((i, n) for i, n in self.q("SELECT id, name FROM forumer_forums") if i not in names)
        topics = Counter(forum for forum, in self.q("SELECT forum_id FROM topics"))
        topics.update(forum for forum, in self.q(
            "SELECT forum_id FROM forumer_topics WHERE id NOT IN (SELECT id FROM topics)"))
        table = Table("Forums only one source has", ["Forum", "Id", "Source", "Topics shown"], note=(
            "Tapatalk made Forumer's categories forums of their own; forums made after the migration are "
            "Tapatalk's only too."))
        for forum_id in sorted(tapatalk ^ forumer):
            table.add(names.get(forum_id) or f"Forum {forum_id}", forum_id,
                      "Tapatalk" if forum_id in tapatalk else "Forumer", topics[forum_id])
        self.tables.append(table)

    def topics(self) -> None:
        tapatalk = self.ids("SELECT id FROM topics")
        listed = self.ids("SELECT id FROM forumer_topics")
        with_copies = self.ids("SELECT DISTINCT topic_id FROM forumer_posts "
                               "WHERE html IS NOT NULL AND topic_id IS NOT NULL")
        archived = self.ids("SELECT DISTINCT topic_id FROM forumer_archive_posts")
        self.lost = {(old_id, topic_id) for old_id, topic_id in self.q(
            "SELECT id, topic_id FROM forumer_posts WHERE post_id IS NULL AND html IS NOT NULL "
            "AND topic_id IS NOT NULL AND topic_id NOT IN (SELECT id FROM topics)")}
        lost_topics = {topic_id for _, topic_id in self.lost}
        forumer = listed | with_copies | archived
        self.shown_topics = shown = tapatalk | listed | lost_topics
        sources_row(self.overview, "Topics", shown, tapatalk, forumer)

        table = Table("Topics", SOURCES, note=(
            "Forumer has a topic if a forum listing shows it or the dump has pages of its posts (full or "
            "lite archive). Flags: P poll, A alert (forumer's \"!\" icon), Q question (\"?\"); a Forumer "
            "topic shows as pinned only where Tapatalk lacks it."))
        sources_row(table, "All", shown, tapatalk, forumer)
        sources_row(table, "With posts on the site", shown & (self.ids("SELECT DISTINCT topic_id FROM posts")
                                                              | lost_topics),
                    self.ids("SELECT DISTINCT topic_id FROM posts"), with_copies)
        sources_row(table, "In a forum listing", shown & listed, set(), listed)
        sources_row(table, "With full pages of posts", shown & with_copies, set(), with_copies)
        sources_row(table, "With lite archive pages", shown & archived, set(), archived)
        described = self.ids("SELECT id FROM forumer_topics WHERE description IS NOT NULL AND description <> ''")
        sources_row(table, "With a description", described & shown, set(), described)
        tt_poll = self.ids("SELECT id FROM topics WHERE has_poll") | self.ids("SELECT topic_id FROM polls")
        f_poll = self.ids("SELECT id FROM forumer_topics WHERE has_poll") | self.ids(
            "SELECT topic_id FROM forumer_polls")
        sources_row(table, "Flag P: a poll", (tt_poll | f_poll) & shown, tt_poll, f_poll)
        for label, column in (("Flag A: alert", "alert"), ("Flag Q: question", "question")):
            flagged = self.ids(f"SELECT id FROM forumer_topics WHERE {column}")
            sources_row(table, label, flagged & shown, set(), flagged)
        tt_pinned = self.ids("SELECT id FROM topics WHERE stickied")
        f_pinned = self.ids("SELECT id FROM forumer_topics WHERE pinned")
        sources_row(table, "Pinned", tt_pinned | (f_pinned - tapatalk), tt_pinned, f_pinned)
        locked = self.ids("SELECT id FROM topics WHERE locked")
        sources_row(table, "Locked", locked, locked, set())
        self.tables.append(table)

    # -- posts -------------------------------------------------------------------------------

    def posts(self) -> None:
        tapatalk = self.ids("SELECT id FROM posts")
        copies = self.q("SELECT id, post_id, match, html IS NOT NULL, member_id, author, topic_id "
                        "FROM forumer_posts")
        lost = {old_id for old_id, _ in self.lost}
        key = {old_id: post_id if post_id is not None else ("f", old_id)
               for old_id, post_id, _, has_html, *_ in copies if post_id is not None or has_html}
        full = set(key.values())
        lite = self.ids("SELECT post_id FROM forumer_archive_posts WHERE post_id IS NOT NULL")
        forumer = full | lite
        shown = tapatalk | {("f", old_id) for old_id in lost}
        sources_row(self.overview, "Posts", shown, tapatalk, forumer)

        table = Table("Posts", SOURCES, note=(
            "Forumer's copies are full pages (index.php) or the lite archive's (a/), matched to Tapatalk's "
            "posts by `link`. Posts of the dump that weren't matched show only where Tapatalk lacks their "
            "topic. Guests' names come from Forumer's copies."))
        sources_row(table, "All", shown, tapatalk, forumer)
        sources_row(table, "With a full page copy", shown & full, set(), full)
        sources_row(table, "With a lite archive copy", shown & lite, set(), lite)
        tt_guests = self.ids("SELECT id FROM posts WHERE user_id = 0")
        f_guests = {key[old_id] for old_id, _, _, _, member_id, *_ in copies
                    if member_id is None and old_id in key}
        named = ({key[old_id] for old_id, _, _, _, member_id, author, _ in copies
                  if member_id is None and author and old_id in key}
                 | self.ids("SELECT post_id FROM forumer_archive_posts WHERE post_id IS NOT NULL "
                            "AND author IS NOT NULL"))
        guests = tt_guests | (f_guests & shown)
        sources_row(table, "By guests", guests, tt_guests, f_guests)
        sources_row(table, "By guests, with their name", guests & named, set(), named & (tt_guests | f_guests))
        self.tables.append(table)

        table = Table("Forumer's copies of posts", ["", "Posts"], note=(
            "\"Interpolated\": no copy of the post's own, its old id worked out from its neighbours'."))
        table.add("Full page copies", len(copies))
        table.add("  with their text", sum(bool(has_html) for _, _, _, has_html, *_ in copies))
        for match, count in sorted(Counter(match for _, post_id, match, *_ in copies
                                           if post_id is not None).items(), key=lambda kv: -kv[1]):
            table.add(f"  matched to Tapatalk's by {match}", count)
        table.add("  not matched, in topics Tapatalk lacks (shown)", len(lost))
        table.add("  not matched, in topics Tapatalk has (not shown)", sum(
            1 for old_id, post_id, _, has_html, *_ in copies
            if post_id is None and has_html and old_id not in lost))
        table.add("  known only by their id", sum(1 for _, post_id, _, has_html, *_ in copies
                                                  if post_id is None and not has_html))
        archive = self.q("SELECT post_id FROM forumer_archive_posts")
        table.add("Lite archive copies", len(archive))
        table.add("  matched to Tapatalk's", sum(post_id is not None for post_id, in archive))
        table.add("    of posts with no full page copy", len(lite - full))
        table.add("  not matched", sum(post_id is None for post_id, in archive))
        self.tables.append(table)

    # -- members -----------------------------------------------------------------------------

    def members(self) -> None:
        users = {user_id: name for user_id, name in self.q("SELECT id, name FROM users WHERE id <> 0")}
        tapatalk = set(users)
        columns = ("old_id", "name", "user_id", "match", "listed", "group_name", "title", "signature",
                   "location", "specific_location", "country", "website", "birthday", "interests",
                   "msn", "aim", "yahoo", "icq")
        members = [dict(zip(columns, row)) for row in self.q(f"SELECT {', '.join(columns)} FROM forumer_members")]
        self.member_key = {m["old_id"]: m["user_id"] if m["user_id"] in users else ("f", m["old_id"])
                           for m in members}
        forumer = set(self.member_key.values())
        sources_row(self.overview, "Members", tapatalk | forumer, tapatalk, forumer)

        table = Table("Members", SOURCES, note=(
            "Members only Forumer has get a page of their own. Signatures: Forumer's shows where Tapatalk "
            "has none."))
        sources_row(table, "All", tapatalk | forumer, tapatalk, forumer)
        tt_signature = self.ids(
            "SELECT id FROM users WHERE id <> 0 AND COALESCE(NULLIF(signature_fixed, ''), "
            "NULLIF(signature_source, ''), NULLIF(signature, '')) IS NOT NULL")
        f_signature = {self.member_key[m["old_id"]] for m in members if filled(m["signature"])}
        sources_row(table, "With a signature", tt_signature | f_signature, tt_signature, f_signature)
        self.tables.append(table)

        table = Table("Forumer's members", ["", "Members"], note=(
            "Profile details are only counted. They're personal data: mind them before publishing the site."))
        table.add("All", len(members))
        for match, count in sorted(Counter(m["match"] for m in members if m["match"]).items(),
                                   key=lambda kv: -kv[1]):
            table.add(f"  matched to a Tapatalk user by {match}", count)
        unmatched = [m for m in members if isinstance(self.member_key[m["old_id"]], tuple)]
        table.add("  not matched: pruned before the migration", sum(m["listed"] is not None and not m["listed"]
                                                                      for m in unmatched))
        table.add("  not matched: others", sum(m["listed"] is None or bool(m["listed"]) for m in unmatched))
        table.add("Tapatalk users only known by their id, named by Forumer", sum(
            1 for m in members if m["user_id"] in users and users[m["user_id"]] == str(m["user_id"])
            and filled(m["name"])))
        for label, fields in (("With a member title", ("title",)), ("With a main group", ("group_name",)),
                              ("With a location", ("location", "specific_location", "country")),
                              ("With a website", ("website",)), ("With a birthday", ("birthday",)),
                              ("With messengers", ("msn", "aim", "yahoo", "icq")),
                              ("With interests", ("interests",))):
            table.add(label, sum(any(filled(m[f]) and str(m[f]).strip().lower() not in ("http://", "https://")
                                     for f in fields) for m in members))
        self.tables.append(table)

    def avatars(self) -> None:
        tapatalk = {user_id: data for user_id, data in self.q(
            "SELECT u.id, a.data FROM users u JOIN avatars a ON a.id = u.avatar_id WHERE u.id <> 0")}
        forumer = {self.member_key[old_id]: data for old_id, data in self.q(
            "SELECT old_id, data FROM forumer_avatars WHERE data IS NOT NULL") if old_id in self.member_key}
        sources_row(self.overview, "Members with an avatar", set(tapatalk) | set(forumer), set(tapatalk), set(forumer))
        both = set(tapatalk) & set(forumer)
        same = sum(tapatalk[key] == forumer[key] for key in both)
        table = Table("Avatars", ["", "Members"], note=(
            "Members with both show both, unless they're the same file."))
        table.add("With a Tapatalk avatar", len(tapatalk))
        table.add("With a Forumer-era avatar recovered", len(forumer))
        table.add("  the same file as their Tapatalk avatar", same)
        table.add("  another than their Tapatalk avatar (both shown)", len(both) - same)
        table.add("  members with no Tapatalk avatar", len(set(forumer) - set(tapatalk)))
        table.add("With a Forumer-era avatar not recovered", self.q(
            "SELECT COUNT(*) FROM forumer_members m WHERE avatar_url IS NOT NULL AND avatar_url <> '' AND NOT EXISTS "
            "(SELECT 1 FROM forumer_avatars a WHERE a.old_id = m.old_id AND a.data IS NOT NULL)")[0][0])
        table.add("Avatar images shown", len(tapatalk) + len(forumer) - same)
        self.tables.append(table)

    # -- polls, attachments, smileys, images ---------------------------------------------------

    def polls(self) -> None:
        tapatalk = {topic_id: votes for topic_id, votes in self.q("SELECT topic_id, vote_count FROM polls")}
        forumer = {topic_id: votes for topic_id, votes in self.q(
            "SELECT topic_id, vote_count FROM forumer_polls")}
        shown = (set(tapatalk) | set(forumer)) & self.shown_topics
        sources_row(self.overview, "Polls", shown, set(tapatalk), set(forumer))
        table = Table("Polls", ["", "Topics"])
        table.add("With the poll's results", len(shown))
        table.add("  Forumer's copy shown: Tapatalk lacks it", len(shown & (set(forumer) - set(tapatalk))))
        table.add("  Forumer's copy shown: more votes than Tapatalk's", sum(
            (forumer[t] or 0) > (tapatalk[t] or 0) for t in set(forumer) & set(tapatalk)))
        flagged = (self.ids("SELECT id FROM topics WHERE has_poll") | self.ids(
            "SELECT id FROM forumer_topics WHERE has_poll")) & self.shown_topics
        table.add("Known to have a poll, results lost", len(flagged - shown))
        self.tables.append(table)

    def attachments(self) -> None:
        # Tapatalk's: the ones its posts and signatures use (`metanet attachments` adds the dump's
        # own, with no uses)
        everything = self.q("SELECT id, kind, data IS NOT NULL, recovered_from, uses FROM attachments")
        rows = [row[:4] for row in everything if row[4]]
        files = {row[0]: row[1:4] for row in everything}
        tapatalk = {row[0] for row in rows}
        boxes = self.q("SELECT old_post_id, ref, kind, attachment_id, data IS NOT NULL FROM forumer_attachments")
        lost_posts = {old_id for old_id, _ in self.lost}
        key = {(old_post_id, ref): attachment_id if attachment_id is not None else ("f", old_post_id, ref)
               for old_post_id, ref, _, attachment_id, _ in boxes}
        forumer = set(key.values())
        shown = tapatalk | {key[old_post_id, ref] for old_post_id, ref, *_ in boxes if old_post_id in lost_posts}
        sources_row(self.overview, "Attachments", shown, tapatalk, forumer)

        table = Table("Attachments", ["", "Files", "Kept", "From the Wayback Machine", "From the dump", "Lost"],
                      note=("Tapatalk's are the files its posts and signatures link or embed. Forumer's are the "
                            "dump's attachment boxes; only the ones of posts Tapatalk lacks add files."))
        for kind, label in (("upload", "Tapatalk: uploads (/attach/ma/, uploads/)"),
                            ("attach-id", "Tapatalk: downloads by id (act=Attach)")):
            of_kind = [row for row in rows if row[1] == kind]
            wayback = sum(bool(has) and (source or "").startswith(WAYBACK) for _, _, has, source in of_kind)
            dump = sum(bool(has) and (source or "").startswith(DUMP) for _, _, has, source in of_kind)
            kept = sum(bool(has) for _, _, has, _ in of_kind)
            table.add(label, len(of_kind), kept, wayback, dump, len(of_kind) - kept)
        for kind in ("file", "image"):
            of_kind = [box for box in boxes if box[2] == kind and box[3] not in tapatalk and box[0] in lost_posts]
            wayback = dump = 0
            for _, _, _, attachment_id, has_data in of_kind:
                _, kept, source = files.get(attachment_id, (None, False, None))
                wayback += bool(kept) and (source or "").startswith(WAYBACK)
                dump += bool(has_data) or (bool(kept) and (source or "").startswith(DUMP))
            table.add(f"Forumer only: {kind}s of posts Tapatalk lacks", len(of_kind), wayback + dump,
                      wayback, dump, len(of_kind) - wayback - dump)
        self.tables.append(table)

    def smileys(self) -> None:
        rows = self.q("SELECT url, host, data IS NOT NULL FROM smilies")
        tapatalk = {url for url, _, _ in rows}
        codes = {normalize_url(url): code for url, code in self.q("SELECT url, code FROM forumer_emoticons")}
        forumer = set(codes)
        sources_row(self.overview, "Smileys", tapatalk, tapatalk, forumer)
        table = Table("Smileys", ["Host", "Smileys", "Image kept", "Titled with Forumer's code"], note=(
            "Smileys of the posts and signatures, wherever they were hosted. Forumer's emoticon list gives "
            "the codes members typed; the ones no post uses aren't shown."))
        for host in sorted({host for _, host, _ in rows}):
            of_host = [row for row in rows if row[1] == host]
            table.add(host, len(of_host), sum(bool(kept) for _, _, kept in of_host),
                      sum(bool(codes.get(url)) for url, _, _ in of_host))
        self.tables.append(table)

    def images(self) -> None:
        columns = list(SCOPE_USES.values())
        rows = self.q(f"SELECT status, uses, {', '.join(columns)} FROM embedded_images")
        table = Table("Embedded images", ["Embedded in", "Images", "Uses", "Kept from the original host",
                                          "Kept from the Wayback Machine", "Missing", "Not looked for yet"],
                      note="Images embedded with [img] from other hosts (see `metanet images`).")
        groups = defaultdict(list)
        for status, uses, *counts in rows:
            where = " and ".join(scope for scope, n in zip(SCOPE_USES, counts) if n) or "nothing any more"
            groups[where].append((status, uses))
        for where in sorted(groups):
            items = groups[where]
            table.add(where, len(items), sum(uses or 0 for _, uses in items),
                      sum(s == "live" for s, _ in items), sum(s == "archived" for s, _ in items),
                      sum(s == "missing" for s, _ in items), sum(s is None for s, _ in items))
        self.tables.append(table)

    def quotes(self) -> None:
        table = Table("Quotes", ["", "Quotes"], note="Quotes in Tapatalk's posts (see `metanet quotes`).")
        total, linked = self.q("SELECT COUNT(*), COUNT(quoted_post_id) FROM quotes")[0]
        table.add("All", total)
        table.add("  linked to the post they quote", linked)
        self.tables.append(table)

    def files(self) -> None:
        table = Table("Files kept in the database", ["", "Files", "MB"])
        for label, sql in (
                ("Avatars (Tapatalk)", "SELECT COUNT(*), SUM(LENGTH(data)) FROM avatars"),
                ("Avatars (Forumer)", "SELECT COUNT(data), SUM(LENGTH(data)) FROM forumer_avatars"),
                ("Smileys", "SELECT COUNT(data), SUM(LENGTH(data)) FROM smilies"),
                ("Attachments", "SELECT COUNT(data), SUM(LENGTH(data)) FROM attachments"),
                ("Attachments only the dump has", "SELECT COUNT(data), SUM(LENGTH(data)) FROM forumer_attachments "
                                            "WHERE attachment_id IS NULL"),
                ("Embedded images", "SELECT COUNT(data), SUM(LENGTH(data)) FROM embedded_images")):
            count, size = self.q(sql)[0]
            table.add(label, count, f"{(size or 0) / 1048576:,.1f}")
        self.tables.append(table)


def metanet_stats(db: Database) -> list[Table]:
    db.create_schema(TABLES)
    return Stats(db).run()
