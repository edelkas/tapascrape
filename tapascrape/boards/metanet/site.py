"""The static site (site.build), with what the Forumer dump adds: `tapascrape metanet site`.

- Forums and topics Tapatalk never got, with the posts the dump has of them
  (forumer's HTML turned into BBCode and run through `fix`, like migrated
  posts). Their posts are keyed by their old ids, anchored #o<old id>.
- Topic descriptions, in a column of the topic tables.
- Members: the forumer names of the accounts Tapatalk only knows by their id;
  members Tapatalk lacks get a page of their own (u/f<old id>.html); profile
  pages add the forumer id, location, website, birthday, messengers and
  interests (personal data: mind it before publishing the site), and the
  forumer-era avatar when it isn't the Tapatalk one.
- Guest posts show the name forumer showed for them.
- Links to old post ids ([ts:topic ... old_post=N]) point at that post.
- Smileys are titled with the code members typed for them.
- Polls the dump has (forumer_polls) when Tapatalk lacks them, or when the
  dump's copy has more votes; topics the dump shows a poll for get the P flag.
- Links to NUMA (numa.notdot.net, dead) go to its new home, nmaps.net
  (numa_url); their text stays as written.
"""

import logging
import re
import sys
from collections import defaultdict
from html import unescape

from tapascrape.boards.metanet.schema import TABLES
from tapascrape.content.fix import FIXES, fix_source, load_context
from tapascrape.content.smilies import normalize_url
from tapascrape.db.base import Database
from tapascrape.net.wayback import image_type
from tapascrape.parse.bbcode import html_to_bbcode
from tapascrape.site.build import (EXTENSIONS, BoardLinks, Forum, Poll, PostRef, SiteBuilder, Topic, User,
                                   as_datetime, number, plain)
from tapascrape.site.render import escape, safe_url

log = logging.getLogger(__name__)

DEEP_RECURSION = 20000      # Python-to-Python calls don't use the C stack (3.12+)
ATTACHMENT_KEYS = 10 ** 9  # attachments only the dump has: this + their post's old id
MESSENGERS = (("msn", "MSN"), ("aim", "AIM"), ("yahoo", "Yahoo"), ("icq", "ICQ"))
NUMA = re.compile(r"(?:https?://)?(?:www\.)?numa\.notdot\.net(?=[/?#]|$)", re.IGNORECASE)
NMAPS = "https://www.nmaps.net"


def numa_url(url: str) -> str:
    """A NUMA link, pointed at nmaps.net: the same paths, but maps lost their "/map"
    (/map/85674 -> /85674) and author searches are queries (browse?author=X ->
    browse?q=author:X, the other parameters kept as written)."""
    m = NUMA.match(url)
    if m is None:
        return url
    rest = url[m.end():]
    rest, hash_, fragment = rest.partition("#")
    path, mark, query = rest.partition("?")
    path = re.sub(r"^/map/(?=\d)", "/", path, flags=re.IGNORECASE)
    if path.rstrip("/").lower() == "/browse" and query:
        params = query.split("&")
        authors = [p.partition("=")[2] for p in params if p.partition("=")[0].lower() == "author"]
        searches = [i for i, p in enumerate(params) if p.partition("=")[0].lower() == "q"]
        terms = "+".join(f"author:{author}" for author in authors if author)
        out = []
        for param in params:
            key = param.partition("=")[0].lower()
            if key == "author":  # the first one becomes the query, if there's no query already
                if terms and not searches:
                    out.append(f"q={terms}")
                    terms = ""
            elif key == "q" and terms:  # joins a query of its own
                out.append(f"{param}+{terms}" if param.partition("=")[2] else f"q={terms}")
                terms = ""
            else:
                out.append(param)
        query = "&".join(out)
        mark = mark if query else ""
    return NMAPS + path + mark + query + hash_ + fragment


class MetanetLinks(BoardLinks):
    def topic(self, topic_id, attrs):
        old_post = attrs.get("old_post", "")
        key = self.site.old_posts.get(int(old_post)) if old_post.isdigit() else None
        if key is not None and key in self.board.posts:
            return self.board.posts[key].href
        return super().topic(topic_id, attrs)

    def url(self, url):
        return numa_url(url)


class MetanetSite(SiteBuilder):
    def __init__(self, db: Database, out, title: str):
        self.members: dict[int, dict] = {}         # old id -> forumer_members row
        self.member_of: dict[int, dict] = {}       # user key -> their forumer_members row
        self.authors: dict[int, str] = {}          # post key -> the name forumer showed (guests)
        self.old_posts: dict[int, int] = {}        # old post id -> post key
        self.added_forums: set[int] = set()        # forums only the dump has
        self.signatures: list[tuple[User, str]] = []  # forumer signatures to convert
        super().__init__(db, out, title)

    def links(self):
        return MetanetLinks(self)

    def build(self) -> dict[str, int]:
        self.db.create_schema(TABLES)
        return super().build()

    # -- loading -------------------------------------------------------------------------------

    def load(self) -> None:
        super().load()
        self.load_forums()
        self.load_members()
        self.load_posts()
        self.load_topics()
        self.load_polls()
        self.fix_context = load_context(self.db)  # links to the topics and forums added too
        self.fix_context.topics |= set(self.board.topics)
        self.fix_context.forums |= set(self.board.forums)
        for user, signature in self.signatures:
            user.signature = self.forumer_bbcode(signature)

    def load_forums(self) -> None:
        board = self.board
        by_name = {forum.name.casefold(): forum.id for forum in board.forums.values()}
        rows = self.db.query("SELECT id, name, description, parent_id, category FROM forumer_forums "
                             "WHERE NOT in_tapatalk")
        missing = {row[0] for row in rows}
        for forum_id, name, description, parent_id, category in rows:
            if parent_id not in board.forums and parent_id not in missing:
                # right in a category: Tapatalk made categories forums of the same name
                parent_id = by_name.get((category or "").casefold())
            board.forums[forum_id] = Forum(forum_id, parent_id, name or f"Forum {forum_id}",
                                           description or "", None, None, None)
            self.added_forums.add(forum_id)

    def load_members(self) -> None:
        board = self.board
        columns = ("old_id", "name", "user_id", "group_name", "title", "joined_at", "post_count",
                   "country", "signature", "birthday", "location", "specific_location", "interests",
                   "website", "msn", "aim", "yahoo", "icq")
        for row in self.db.query(f"SELECT {', '.join(columns)} FROM forumer_members"):
            member = dict(zip(columns, row))
            self.members[member["old_id"]] = member
            user = board.users.get(member["user_id"])
            if user is None:  # not on Tapatalk: a page of their own
                user = User(-member["old_id"], member["name"] or f"member {member['old_id']}",
                            None, as_datetime(member["joined_at"]), None, member["post_count"],
                            None, page=f"u/f{member['old_id']}.html")
                board.users[user.id] = user
            elif member["name"] and user.name == str(user.id):
                user.name = member["name"]  # Tapatalk only knew them by their id
            if not user.signature and member["signature"]:
                self.signatures.append((user, member["signature"]))
            self.member_of[user.id] = member

    def user_key(self, member_id: int | None) -> int:
        member = self.members.get(member_id) if member_id is not None else None
        if member is None:
            return 0
        return member["user_id"] if member["user_id"] in self.board.users else -member["old_id"]

    def load_posts(self) -> None:
        board = self.board
        # guests' names, on posts Tapatalk has
        for post_id, author in self.db.query(
                "SELECT a.post_id, a.author FROM forumer_archive_posts a JOIN posts p ON p.id = a.post_id "
                "WHERE p.user_id = 0 AND a.author IS NOT NULL"):
            self.authors[post_id] = author
        for old_id, post_id, author, user_id in self.db.query(
                "SELECT f.id, f.post_id, f.author, p.user_id FROM forumer_posts f "
                "JOIN posts p ON p.id = f.post_id"):
            self.old_posts[old_id] = post_id
            if user_id == 0 and author:
                self.authors[post_id] = author
        # posts of topics Tapatalk never got
        rows = self.db.query("SELECT id, topic_id, member_id, author, posted_at, html FROM forumer_posts "
                             "WHERE post_id IS NULL AND html IS NOT NULL AND topic_id IS NOT NULL "
                             "AND topic_id NOT IN (SELECT id FROM topics) ORDER BY posted_at, id")
        attachments = defaultdict(list)
        for row in self.db.query("SELECT old_post_id, ref, kind, name, attachment_id, content_type, data "
                                 "FROM forumer_attachments WHERE old_post_id IN (SELECT id FROM forumer_posts "
                                 "WHERE post_id IS NULL)"):
            attachments[row[0]].append(row[1:])
        self.pending_attachments = attachments
        position: dict[int, int] = defaultdict(int)
        self.lost_posts = []
        for old_id, topic_id, member_id, author, posted_at, body in rows:
            position[topic_id] += 1
            post = PostRef(-old_id, topic_id, self.user_key(member_id), as_datetime(posted_at),
                           position[topic_id], anchor=f"o{old_id}", label=f"#{old_id} (forumer)")
            board.posts[post.id] = post
            self.old_posts[old_id] = post.id
            if not post.user_id and author:
                self.authors[post.id] = author
            self.lost_posts.append((post, body))

    def load_topics(self) -> None:
        board = self.board
        lost: dict[int, list[PostRef]] = defaultdict(list)
        for post, _ in self.lost_posts:
            lost[post.topic_id].append(post)
        known = set()
        for topic_id, forum_id, title, description, started_at, pinned, has_poll in self.db.query(
                "SELECT id, forum_id, title, description, started_at, pinned, has_poll FROM forumer_topics"):
            known.add(topic_id)
            if topic_id in board.topics:
                board.topics[topic_id].description = description or None
            else:
                board.topics[topic_id] = self.lost_topic(topic_id, forum_id, title, description,
                                                         as_datetime(started_at), bool(pinned), lost[topic_id])
            board.topics[topic_id].has_poll = board.topics[topic_id].has_poll or bool(has_poll)
        for topic_id in lost.keys() - known:
            board.topics[topic_id] = self.lost_topic(topic_id, None, None, None, None, False, lost[topic_id])
        # forums Tapatalk lacks: their counts and last post, from what the dump has
        for forum_id in self.added_forums:
            forum = board.forums[forum_id]
            topics = [t for t in board.topics.values() if t.forum_id == forum.id]
            forum.post_count = sum(t.post_count or 0 for t in topics)
            lasts = [t.last_post_id for t in topics if t.last_post_id is not None]
            forum.last_post_id = max(lasts, key=self.last_time, default=None)

    def load_polls(self) -> None:
        """The dump's polls: kept when Tapatalk lacks the poll, or its copy has fewer votes
        (the dump's would then be the later snapshot)."""
        polls = self.board.polls
        for topic_id, *row in self.db.query(
                "SELECT topic_id, title, vote_count, max_options, options FROM forumer_polls"):
            poll = Poll.from_row(*row)
            if topic_id not in polls or (poll.vote_count or 0) > (polls[topic_id].vote_count or 0):
                polls[topic_id] = poll

    def lost_topic(self, topic_id, forum_id, title, description, started_at, pinned, posts) -> Topic:
        first, last = (posts[0], posts[-1]) if posts else (None, None)
        return Topic(topic_id, forum_id, first.user_id if first else None, title or f"Topic {topic_id}",
                     pinned, False, started_at or (first.timestamp if first else None),
                     len(posts) or None, None, last.id if last else None, description or None)

    # -- text ----------------------------------------------------------------------------------

    def forumer_bbcode(self, html: str) -> str:
        """Forumer's rendered HTML -> BBCode with the migration fixes (quotes, smileys, uploads...)."""
        limit = sys.getrecursionlimit()
        try:
            try:
                bbcode = html_to_bbcode(html)
            except RecursionError:  # tags never closed, nested hundreds deep
                sys.setrecursionlimit(DEEP_RECURSION)
                bbcode = html_to_bbcode(html)
        except RecursionError:  # deeper still: keep the text
            text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
            bbcode = unescape(re.sub(r"<[^>]*>", "", text)).replace("[", "&#91;")
            log.warning("forumer post HTML nested too deep to convert; kept as plain text")
        finally:
            sys.setrecursionlimit(limit)
        return fix_source(bbcode, FIXES, self.fix_context)[0]

    def write_files(self) -> None:
        super().write_files()
        board = self.board
        # the text of posts only the dump has, now that attachments are known
        for post, body in self.lost_posts:
            post.text = self.forumer_bbcode(body) + self.attachment_blocks(-post.id)
        # forumer-era avatars, when they aren't the Tapatalk one
        for old_id, data in self.db.query("SELECT old_id, data FROM forumer_avatars WHERE data IS NOT NULL"):
            user = board.users.get(self.user_key(old_id))
            if user is None:
                continue
            if any((self.out / href).read_bytes() == data for href in user.avatars):
                continue
            user.avatars.append(self.write_file(
                f"files/avatars/f{old_id}{EXTENSIONS.get(image_type(data), '')}", data))
        # smileys: titled with the code members typed
        codes = {normalize_url(url): code for url, code in self.db.query(
            "SELECT url, code FROM forumer_emoticons WHERE code IS NOT NULL")}
        for smiley_id, url in self.db.query("SELECT id, url FROM smilies"):
            if (code := codes.get(url) or codes.get(normalize_url(url))) and smiley_id in board.smilies:
                board.smilies[smiley_id].label = code

    def attachment_blocks(self, old_post_id: int) -> str:
        """[ts:attachment] sentinels for the attachment box of a post only the dump has."""
        blocks = []
        for ref, kind, name, attachment_id, content_type, data in self.pending_attachments.get(old_post_id, []):
            if attachment_id is not None and attachment_id in self.board.attachments:
                key = attachment_id
            else:
                key = ATTACHMENT_KEYS + old_post_id + len(blocks)
                self.add_attachment(key, name or (ref.rsplit("/", 1)[-1] if kind == "image" else None),
                                    content_type, data, f"f{old_post_id}-{len(blocks)}")
            blocks.append(f"\n[ts:attachment={key}]")
        return "".join(blocks)

    # -- what's shown --------------------------------------------------------------------------

    def author_name(self, post: PostRef) -> str:
        if post.user_id not in self.board.users and post.id in self.authors:
            return self.authors[post.id]
        return super().author_name(post)

    def post_author(self, post: PostRef, root: str) -> str:
        if post.user_id not in self.board.users and post.id in self.authors:
            return f'{escape(self.authors[post.id])} <span class="guest">(guest)</span>'
        return super().post_author(post, root)

    def user_columns(self):
        def old_id(user: User, root: str) -> str:
            member = self.member_of.get(user.id)
            return number(member["old_id"]) if member else ""

        return [("Old ID", "id", old_id)] + super().user_columns()

    def user_fields(self, user: User, root: str) -> list[tuple[str, str]]:
        rows = super().user_fields(user, root)
        member = self.member_of.get(user.id)
        if member is None:
            return rows
        rows.insert(1, ("Forumer ID", number(member["old_id"])))
        labels = [label for label, _ in rows]
        if member["title"] and member["title"] != user.rank:  # they often match
            rows.insert(labels.index("Rank"), ("Old title", plain(member["title"])))
            labels = [label for label, _ in rows]
        rows.insert(labels.index("Groups"), ("Main group", plain(member["group_name"])))
        places = []
        for place in (member["specific_location"], member["location"], member["country"]):
            if place and place.strip() and place.strip() not in places:
                places.append(place.strip())
        website = (member["website"] or "").strip()
        href = safe_url(website) if website and website.lower() not in ("http://", "https://") else None
        messengers = [f"{label}: {member[key].strip()}" for key, label in MESSENGERS
                      if member[key] and member[key].strip()]
        rows += [("Location", plain(", ".join(places))),
                 ("Website", f'<a href="{escape(href)}">{plain(website)}</a>' if href else ""),
                 ("Birthday", plain(member["birthday"])),
                 ("Socials", plain(" · ".join(messengers))),
                 ("Interests", plain(member["interests"]))]
        return rows


def build_metanet_site(db: Database, out, title: str) -> dict[str, int]:
    return MetanetSite(db, out, title).build()
