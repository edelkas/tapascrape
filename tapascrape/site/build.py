"""A static HTML site to browse an archived board: `tapascrape site`.

    index.html          the board's forum tree
    f/<id>.html         a forum: its subforum tree, then its topics
    t/<id>.html         a topic: its poll, then every post, oldest first (anchors #p<post id>)
    users.html          every user (linked from every page's header)
    u/<id>.html         a user's profile
    u/<id>-posts.html   all their posts, oldest first
    files/...           smileys, attachments and avatars stored in the database
    style.css           every bit of styling; edit it freely

Plain HTML and CSS, no JavaScript, one page per forum/topic/user, no pagination.
Everything is plain text except post bodies and signatures, rendered from their
BBCode (see render.py). Quotes link to the post they quote (the quotes table).

SiteBuilder loads the board into a small model, then writes it. Boards with more
to say (other names, extra profile fields, posts and topics from elsewhere)
subclass it: `load` can add to the model (posts can carry their own text, an
anchor and a label; users their own page and several avatars), and the small
hooks (user_name, post_author, user_fields, topic_flags, post_text...) change what's shown.
"""

import html
import json
import logging
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from importlib import resources
from pathlib import Path

from tapascrape.crawl.progress import Progress
from tapascrape.db.base import Database
from tapascrape.net.wayback import image_type
from tapascrape.parse.bbcode import html_to_bbcode
from tapascrape.site.render import Linked, Links, Quoted, Renderer, escape

log = logging.getLogger(__name__)

EXTENSIONS = {"image/gif": ".gif", "image/png": ".png", "image/jpeg": ".jpg", "image/bmp": ".bmp",
              "image/webp": ".webp"}
UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


# -- model ---------------------------------------------------------------------------------------

@dataclass
class PostRef:
    id: int                          # the key in Board.posts (the board's id, for its own posts)
    topic_id: int
    user_id: int | None
    timestamp: datetime | None
    index: int | None
    anchor: str = ""                 # the post's id on its topic page; default p<id>
    label: str = ""                  # how its header names it; default #<id>
    text: str | None = None          # its BBCode, for posts that aren't in the posts table

    def __post_init__(self):
        self.anchor = self.anchor or f"p{self.id}"
        self.label = self.label or f"#{self.id}"

    @property
    def href(self) -> str:
        return f"t/{self.topic_id}.html#{self.anchor}"


@dataclass
class Topic:
    id: int
    forum_id: int
    user_id: int | None
    name: str
    stickied: bool
    locked: bool
    created_at: datetime | None
    post_count: int | None
    view_count: int | None
    last_post_id: int | None
    description: str | None = None
    first_post_id: int | None = None  # set by link_model
    has_poll: bool = False            # also set by link_model, for topics with a poll in Board.polls


@dataclass
class Poll:
    title: str | None
    vote_count: int | None
    max_options: int | None          # how many options a member could pick; None: unknown
    options: list[tuple[str, int]]   # (text, votes), in order

    @classmethod
    def from_row(cls, title, vote_count, max_options, options) -> "Poll":
        return cls(title, vote_count, max_options,
                   [(o.get("text") or "", o.get("votes") or 0) for o in json.loads(options or "[]")])


@dataclass
class Forum:
    id: int
    parent_id: int | None
    name: str
    description: str
    post_count: int | None
    view_count: int | None
    last_post_id: int | None
    children: list["Forum"] = field(default_factory=list)
    topics: list[Topic] = field(default_factory=list)


@dataclass
class User:
    id: int                          # the key in Board.users
    name: str
    rank: str | None
    joined_at: datetime | None
    last_active_at: datetime | None
    post_count: int | None
    signature: str | None            # BBCode
    avatars: list[str] = field(default_factory=list)  # hrefs from the site's root
    first_post_id: int | None = None
    last_post_id: int | None = None
    groups: list[str] = field(default_factory=list)
    page: str = ""                   # default u/<id>.html
    board_id: int | None = None      # the board's id for them; default the key

    post_ids: list[int] = field(default_factory=list)  # their posts we have, oldest first

    def __post_init__(self):
        self.page = self.page or f"u/{self.id}.html"
        if self.board_id is None and self.id > 0:
            self.board_id = self.id

    @property
    def posts_page(self) -> str:
        return self.page.removesuffix(".html") + "-posts.html"


@dataclass
class Board:
    title: str
    forums: dict[int, Forum] = field(default_factory=dict)
    roots: list[Forum] = field(default_factory=list)
    topics: dict[int, Topic] = field(default_factory=dict)
    users: dict[int, User] = field(default_factory=dict)
    posts: dict[int, PostRef] = field(default_factory=dict)
    quotes: dict[tuple[int, int], int] = field(default_factory=dict)  # (post, position) -> quoted post
    polls: dict[int, Poll] = field(default_factory=dict)  # topic id -> its poll
    smilies: dict[int, Linked] = field(default_factory=dict)
    attachments: dict[int, Linked] = field(default_factory=dict)


def as_datetime(value) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def iso(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ") if value else ""


def plain(value) -> str:
    """Plain text for a table cell or a title (names may carry character references)."""
    return html.escape(" ".join(html.unescape(str(value or "")).split()), quote=False)


def stripped(value) -> str:
    """Plain text of what may be HTML (forum descriptions)."""
    return plain(re.sub(r"<[^>]*>", " ", str(value or "")))


def number(value) -> str:
    return "" if value is None else f"{value:,}"


# -- links ---------------------------------------------------------------------------------------

class BoardLinks(Links):
    def __init__(self, site: "SiteBuilder"):
        self.site = site
        self.board = site.board

    def smiley(self, smiley_id):
        return self.board.smilies.get(smiley_id)

    def attachment(self, attachment_id):
        return self.board.attachments.get(attachment_id)

    def topic(self, topic_id, attrs):
        return f"t/{topic_id}.html" if topic_id in self.board.topics else None

    def forum(self, forum_id):
        return f"f/{forum_id}.html" if forum_id in self.board.forums else None

    def quoted(self, post_id, position):
        quoted = self.board.quotes.get((post_id, position))
        post = self.board.posts.get(quoted) if quoted is not None else None
        if post is None:
            return None
        return Quoted(post.href, self.site.author_name(post), iso(post.timestamp))


# -- builder -------------------------------------------------------------------------------------

class SiteBuilder:
    def __init__(self, db: Database, out: Path, title: str):
        self.db = db
        self.out = Path(out)
        self.board = Board(title)
        self.renderer = Renderer(self.links())

    def links(self) -> Links:
        return BoardLinks(self)

    def build(self) -> dict[str, int]:
        self.load()
        self.link_model()
        for sub in ("f", "t", "u", "files/smilies", "files/attachments", "files/avatars"):
            (self.out / sub).mkdir(parents=True, exist_ok=True)
        self.write_files()
        shutil.copyfile(resources.files("tapascrape.site").joinpath("style.css"), self.out / "style.css")
        self.write("index.html", "", [], self.forum_table(self.board.roots, ""), self.board.title)
        for forum in self.board.forums.values():
            self.write_forum(forum)
        self.write_topics()
        self.write_users()
        progress = Progress("site: users", len(self.board.users))
        for user in self.board.users.values():
            self.write_user(user)
            self.write_user_posts(user)
            progress.advance()
        return {"forums": len(self.board.forums), "topics": len(self.board.topics),
                "users": len(self.board.users), "posts": len(self.board.posts)}

    # -- loading -------------------------------------------------------------------------------

    def load(self) -> None:
        board, q = self.board, self.db.quote_ident
        for row in self.db.query("SELECT id, parent_id, name, description, post_count, view_count, "
                                 "last_post_id FROM forums"):
            board.forums[row[0]] = Forum(*row[:3], row[3] or "", *row[4:])
        for row in self.db.query("SELECT id, forum_id, user_id, name, stickied, locked, created_at, "
                                 "post_count, view_count, last_post_id, has_poll FROM topics"):
            topic = Topic(*row[:4], bool(row[4]), bool(row[5]), as_datetime(row[6]), *row[7:10])
            topic.has_poll = bool(row[10])
            board.topics[topic.id] = topic
        for topic_id, *poll in self.db.query(
                "SELECT topic_id, title, vote_count, max_options, options FROM polls"):
            board.polls[topic_id] = Poll.from_row(*poll)
        for row in self.db.query(f"SELECT id, topic_id, user_id, timestamp, {q('index')} FROM posts"):
            board.posts[row[0]] = PostRef(row[0], row[1], row[2], as_datetime(row[3]), row[4])
        for row in self.db.query("SELECT id, name, rank, joined_at, last_active_at, post_count, "
                                 "signature_fixed, signature_source, signature FROM users"):
            signature = row[6] or row[7] or (html_to_bbcode(row[8]) if row[8] else None)
            board.users[row[0]] = User(row[0], row[1], row[2], as_datetime(row[3]), as_datetime(row[4]),
                                       row[5], signature)
        board.users.pop(0, None)  # guests
        for user_id, group_id, name in self.db.query(
                "SELECT gu.user_id, gu.group_id, g.name FROM group_users gu "
                "LEFT JOIN groups g ON g.id = gu.group_id ORDER BY gu.group_id"):
            if user_id in board.users:
                board.users[user_id].groups.append(name or f"group {group_id}")
        board.quotes = {(post_id, position): quoted for post_id, position, quoted in self.db.query(
            "SELECT post_id, position, quoted_post_id FROM quotes WHERE quoted_post_id IS NOT NULL")}

    def link_model(self) -> None:
        """Tie the loaded model together (after `load`, and whatever subclasses add to it)."""
        board = self.board
        board.roots.clear()
        for forum in board.forums.values():
            forum.children.clear()
            forum.topics.clear()
        for forum in board.forums.values():
            parent = board.forums.get(forum.parent_id)
            (parent.children if parent else board.roots).append(forum)
        for topic in board.topics.values():
            topic.has_poll = topic.has_poll or topic.id in board.polls
            if topic.forum_id in board.forums:
                board.forums[topic.forum_id].topics.append(topic)
        for user in board.users.values():
            user.first_post_id = user.last_post_id = None
            user.post_ids = []
        for post in sorted(board.posts.values(), key=self.chronological):
            topic = board.topics.get(post.topic_id)
            if topic is not None and topic.first_post_id is None:
                topic.first_post_id = post.id
            user = board.users.get(post.user_id)
            if user is not None:
                user.first_post_id = user.first_post_id or post.id
                user.last_post_id = post.id
                user.post_ids.append(post.id)

    @staticmethod
    def chronological(post: PostRef) -> tuple:
        return (post.timestamp is None, post.timestamp or datetime.min, post.index or 0, post.id)

    # -- files ---------------------------------------------------------------------------------

    def write_file(self, href: str, data: bytes) -> str:
        (self.out / href).write_bytes(data)
        return href

    def write_files(self) -> None:
        """Smileys, attachments and avatars stored in the database -> files/."""
        board = self.board
        for smiley_id, name, data in self.db.query("SELECT id, name, data FROM smilies"):
            href = None
            if data is not None:
                href = self.write_file(f"files/smilies/{smiley_id}{EXTENSIONS.get(image_type(data), '')}", data)
            board.smilies[smiley_id] = Linked(href, name or "", True)
        for attachment_id, name, content_type, data in self.db.query(
                "SELECT id, name, content_type, data FROM attachments"):
            self.add_attachment(attachment_id, name, content_type, data, str(attachment_id))
        for user_id, data in self.db.query(
                "SELECT u.id, a.data FROM users u JOIN avatars a ON a.id = u.avatar_id"):
            if (user := board.users.get(user_id)) is not None:
                user.avatars.append(self.write_file(
                    f"files/avatars/{user_id}{EXTENSIONS.get(image_type(data), '')}", data))

    def add_attachment(self, key: int, name: str | None, content_type: str | None, data: bytes | None,
                       prefix: str) -> None:
        name = name or f"attachment{EXTENSIONS.get(content_type or '', '')}"
        href = None
        if data is not None:
            href = self.write_file(f"files/attachments/{prefix}-{UNSAFE_NAME.sub('_', name)}", data)
        self.board.attachments[key] = Linked(href, name, data is not None and image_type(data) is not None)

    # -- hooks for boards that know more ---------------------------------------------------------

    def user_name(self, user_id: int | None) -> str:
        user = self.board.users.get(user_id)
        return user.name if user else "Guest" if not user_id else f"user {user_id}"

    def author_name(self, post: PostRef) -> str:
        """The name of a post's author, as plain text."""
        return self.user_name(post.user_id)

    def post_author(self, post: PostRef, root: str) -> str:
        """HTML naming a post's author (a link to their page, if they have one)."""
        if post.user_id in self.board.users:
            return self.user_link(post.user_id, root)
        return escape(self.author_name(post))

    def topic_starter(self, topic: Topic, root: str) -> str:
        """HTML naming who started a topic (the first post's author, when it's theirs)."""
        if topic.user_id is None:
            return ""  # not known
        first = self.board.posts.get(topic.first_post_id) if topic.first_post_id is not None else None
        if first is not None and first.user_id == topic.user_id:
            return self.post_author(first, root)
        return self.user_link(topic.user_id, root)

    def user_fields(self, user: User, root: str) -> list[tuple[str, str]]:
        """(label, HTML) rows of a user's page, signature aside; empty ones aren't shown."""
        rows = [("ID", number(user.board_id)), ("Name", escape(user.name)), ("Rank", plain(user.rank)),
                ("Groups", plain(", ".join(user.groups))), ("Joined", iso(user.joined_at)),
                ("Last active", iso(user.last_active_at)), ("Posts", self.post_count_link(user, root))]
        for label, post_id in (("First post", user.first_post_id), ("Last post", user.last_post_id)):
            post = self.board.posts.get(post_id) if post_id else None
            topic = self.board.topics.get(post.topic_id) if post else None
            if post is not None:
                name = plain(topic.name) if topic else f"topic {post.topic_id}"
                rows.append((label, f'{iso(post.timestamp)} <a href="{root}{post.href}">{name}</a>'))
        return rows

    def post_text(self, source_fixed: str | None, source: str | None, content: str | None) -> str:
        if source_fixed is not None:
            return source_fixed
        if source is not None:
            return source
        return html_to_bbcode(content) if content else ""

    def user_columns(self) -> list[tuple[str, str, "Callable[[User, str], str]"]]:
        """(heading, cell class, cell HTML) of the users table."""
        def post_date(post_id: int | None, root: str) -> str:
            post = self.board.posts.get(post_id) if post_id else None
            return f'<a href="{root}{post.href}">{iso(post.timestamp)}</a>' if post else ""

        return [("ID", "id", lambda u, root: number(u.board_id)),
                ("Name", "name", lambda u, root: f'<a href="{root}{u.page}">{escape(u.name)}</a>'),
                ("Rank", "", lambda u, root: plain(u.rank)),
                ("Joined", "date", lambda u, root: iso(u.joined_at)),
                ("Last active", "date", lambda u, root: iso(u.last_active_at)),
                ("Posts", "count", self.post_count_link),
                ("First post", "date", lambda u, root: post_date(u.first_post_id, root)),
                ("Last post", "date", lambda u, root: post_date(u.last_post_id, root))]

    # -- pieces --------------------------------------------------------------------------------

    def post_count_link(self, user: User, root: str) -> str:
        """The user's post count (the board's, else how many we have), linking to their posts."""
        count = user.post_count if user.post_count is not None else len(user.post_ids)
        return f'<a href="{root}{user.posts_page}">{number(count)}</a>'

    def user_link(self, user_id: int | None, root: str) -> str:
        name = escape(self.user_name(user_id))
        user = self.board.users.get(user_id)
        return f'<a href="{root}{user.page}">{name}</a>' if user else name

    def last_post_cells(self, post_id: int | None, root: str) -> str:
        post = self.board.posts.get(post_id) if post_id else None
        if post is None:
            return "<td></td><td></td>"
        return (f'<td>{self.post_author(post, root)}</td>'
                f'<td class="date"><a href="{root}{post.href}">{iso(post.timestamp)}</a></td>')

    def last_time(self, post_id: int | None) -> datetime:
        post = self.board.posts.get(post_id) if post_id else None
        return post.timestamp if post and post.timestamp else datetime.min

    def subtree_last(self, forum: Forum) -> datetime:
        return max([self.last_time(forum.last_post_id), *(self.subtree_last(c) for c in forum.children)])

    def crumbs(self, forum_id: int | None) -> list[tuple[str, str]]:
        """(href from the root, name) of a forum and its ancestors, outermost first."""
        chain, seen = [], set()
        forum = self.board.forums.get(forum_id)
        while forum is not None and forum.id not in seen:
            seen.add(forum.id)
            chain.append((f"f/{forum.id}.html", forum.name))
            forum = self.board.forums.get(forum.parent_id)
        return chain[::-1]

    def forum_table(self, forums: list[Forum], root: str) -> str:
        if not forums:
            return ""
        rows: list[str] = []

        def add(level: list[Forum], depth: int) -> None:
            for forum in sorted(level, key=self.subtree_last, reverse=True):
                indent = '<span class="indent"></span>' * depth
                rows.append(
                    f'<tr id="f{forum.id}"><td class="id">{forum.id}</td>'
                    f'<td class="name">{indent}<a href="{root}f/{forum.id}.html">{plain(forum.name)}</a></td>'
                    f'<td class="description">{stripped(forum.description)}</td>'
                    f'<td class="count">{number(len(forum.children))}</td>'
                    f'<td class="count">{number(len(forum.topics))}</td>'
                    f'<td class="count">{number(forum.post_count)}</td>'
                    f'<td class="count">{number(forum.view_count)}</td>'
                    f'{self.last_post_cells(forum.last_post_id, root)}</tr>')
                add(forum.children, depth + 1)

        add(forums, 0)
        return ('<table class="forums"><thead><tr><th>ID</th><th>Forum</th><th>Description</th>'
                '<th>Subforums</th><th>Topics</th><th>Posts</th><th>Views</th><th>Last post by</th>'
                '<th>Last post</th></tr></thead><tbody>\n' + "\n".join(rows) + "\n</tbody></table>")

    def topic_table(self, topics: list[Topic], root: str) -> str:
        if not topics:
            return ""
        # Boards that know topic descriptions get a column for them.
        described = any(t.description for t in self.board.topics.values())
        ordered = sorted(topics, key=lambda t: (t.stickied, self.last_time(t.last_post_id), t.id),
                         reverse=True)
        rows = []
        for topic in ordered:
            on = self.topic_flags(topic)
            flags = "".join(flag if flag in on else "&nbsp;" for flag, _ in self.flag_meanings())
            description = f'<td class="description">{plain(topic.description)}</td>' if described else ""
            rows.append(
                f'<tr id="t{topic.id}"><td class="id"><a href="{root}f/{topic.forum_id}.html#t{topic.id}">'
                f'{topic.id}</a></td><td class="flags">{flags}</td>'
                f'<td class="name"><a href="{root}t/{topic.id}.html">{plain(topic.name)}</a></td>{description}'
                f'<td>{self.topic_starter(topic, root)}</td>'
                f'<td class="date">{iso(topic.created_at)}</td>'
                f'<td class="count">{number(topic.post_count)}</td><td class="count">{number(topic.view_count)}</td>'
                f'{self.last_post_cells(topic.last_post_id, root)}</tr>')
        description_head = "<th>Description</th>" if described else ""
        legend = "&#10;".join(f"{flag}: {html.escape(meaning)}" for flag, meaning in self.flag_meanings())
        return (f'<table class="topics"><thead><tr><th>ID</th><th class="flags" title="{legend}">Flags</th>'
                f'<th>Topic</th>{description_head}'
                '<th>Started by</th><th>Started</th><th>Posts</th><th>Views</th><th>Last post by</th>'
                '<th>Last post</th></tr></thead><tbody>\n' + "\n".join(rows) + "\n</tbody></table>")

    def flag_meanings(self) -> list[tuple[str, str]]:
        """The topic tables' flags, in column order, and what they mean (the column's tooltip)."""
        return [("S", "sticky"), ("L", "locked"), ("P", "has a poll")]

    def topic_flags(self, topic: Topic) -> set[str]:
        """The flags a topic has (see flag_meanings)."""
        return {flag for flag, on in (("S", topic.stickied), ("L", topic.locked), ("P", topic.has_poll))
                if on}

    # -- pages ---------------------------------------------------------------------------------

    def write(self, path: str, root: str, crumbs: list[tuple[str | None, str]], body: str,
              title: str) -> None:
        trail = " &rsaquo; ".join(
            [f'<a href="{root}index.html">Index</a>']
            + [f'<a href="{root}{href}">{plain(name)}</a>' if href else plain(name) for href, name in crumbs])
        page = (f'<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
                f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
                f'<title>{plain(title)}</title>\n<link rel="stylesheet" href="{root}style.css">\n</head>\n'
                f'<body>\n<header><div class="board"><a href="{root}index.html">{plain(self.board.title)}</a>'
                f'</div>{self.shortcuts(root)}<nav class="crumbs">{trail}</nav></header>\n'
                f'<main>\n{body}\n</main>\n</body>\n</html>\n')
        (self.out / path).write_text(page, encoding="utf-8")

    def shortcuts(self, root: str) -> str:
        """The header's links to the board-wide pages."""
        return f'<nav class="shortcuts"><a href="{root}users.html">Users</a></nav>'

    def write_forum(self, forum: Forum) -> None:
        root = "../"
        body = self.forum_table(forum.children, root) + self.topic_table(forum.topics, root)
        crumbs = self.crumbs(forum.id)
        crumbs[-1] = (None, crumbs[-1][1])  # the page itself
        self.write(f"f/{forum.id}.html", root, crumbs, body or '<p class="empty">Nothing here.</p>',
                   f"{forum.name} - {self.board.title}")

    def topic_posts(self) -> dict[int, list[PostRef]]:
        by_topic: dict[int, list[PostRef]] = {topic_id: [] for topic_id in self.board.topics}
        for post in self.board.posts.values():
            by_topic.setdefault(post.topic_id, []).append(post)
        return {topic_id: sorted(posts, key=self.chronological) for topic_id, posts in by_topic.items()}

    def write_topics(self) -> None:
        by_topic = self.topic_posts()
        progress = Progress("site: topics", len(by_topic))
        for topic_id, posts in by_topic.items():
            self.write_topic(topic_id, posts)
            progress.advance()

    def post_blocks(self, posts: list[PostRef], column: str, value: int, root: str) -> list[str]:
        """The posts as on a topic page; the posts table's `column` = `value` has their text."""
        texts = {}
        if any(post.text is None for post in posts):
            texts = {row[0]: self.post_text(*row[1:]) for row in self.db.query(
                "SELECT id, source_fixed, source, CASE WHEN source IS NULL AND source_fixed IS NULL "
                f"THEN content END FROM posts WHERE {column} = ?", (value,))}
        out = []
        for post in posts:
            text = post.text if post.text is not None else texts.get(post.id, "")
            body = self.renderer.render(text, root, post.id)
            out.append(f'<div class="post" id="{post.anchor}"><div class="post-head">'
                       f'<a class="id" href="{root}{post.href}">{escape(post.label)}</a> '
                       f'<span class="index">{"" if post.index is None else post.index}</span> '
                       f'<span class="date">{iso(post.timestamp)}</span> '
                       f'<span class="author">{self.post_author(post, root)}</span></div>'
                       f'<div class="post-body">{body}</div></div>')
        return out

    def poll_block(self, topic: Topic | None) -> str:
        """A topic's poll, above its posts: the question, then each option's votes."""
        poll = self.board.polls.get(topic.id) if topic else None
        if poll is None:
            return ('<div class="poll"><div class="poll-head">Poll <span class="poll-note">'
                    "(its results weren't archived)</span></div></div>" if topic and topic.has_poll else "")
        votes = poll.vote_count if poll.vote_count is not None else sum(v for _, v in poll.options)
        notes = [f"{number(votes)} vote{'' if votes == 1 else 's'}"]
        if poll.max_options and poll.max_options > 1:
            notes.append(f"up to {poll.max_options} choices each")
        rows = "".join(f'<tr><td>{plain(text)}</td><td class="count">{number(count)}</td></tr>'
                       for text, count in poll.options)
        return (f'<div class="poll"><div class="poll-head">Poll: <span class="poll-title">{plain(poll.title)}'
                f'</span> <span class="poll-note">({", ".join(notes)})</span></div>'
                f'<table class="poll-options"><tbody>{rows}</tbody></table></div>')

    def write_topic(self, topic_id: int, posts: list[PostRef]) -> None:
        root = "../"
        out = self.post_blocks(posts, "topic_id", topic_id, root)
        topic = self.board.topics.get(topic_id)
        name = topic.name if topic else f"Topic {topic_id}"
        crumbs = self.crumbs(topic.forum_id if topic else None) + [(None, name)]
        body = "\n".join(out) or '<p class="empty">No posts of this topic were archived.</p>'
        body = self.poll_block(topic) + body
        self.write(f"t/{topic_id}.html", root, crumbs, body, f"{name} - {self.board.title}")

    def write_user(self, user: User) -> None:
        root = "../"
        rows = "".join(f'<tr><th>{label}</th><td>{value}</td></tr>'
                       for label, value in self.user_fields(user, root) if value)
        avatars = "".join(f'<img class="avatar" src="{root}{href}" alt="">' for href in user.avatars)
        avatars = f'<div class="avatars">{avatars}</div>' if avatars else ""
        signature = (f'<div class="signature">{self.renderer.render(user.signature, root)}</div>'
                     if user.signature and user.signature.strip() else "")
        body = f'<div class="user">{avatars}<table class="profile">{rows}</table></div>{signature}'
        self.write(user.page, root, [("users.html", "Users"), (None, user.name)], body,
                   f"{user.name} - {self.board.title}")

    def write_user_posts(self, user: User) -> None:
        root = "../"
        posts = [self.board.posts[post_id] for post_id in user.post_ids]
        out = self.post_blocks(posts, "user_id", user.id, root)
        body = "\n".join(out) or '<p class="empty">No posts of theirs were archived.</p>'
        self.write(user.posts_page, root, [("users.html", "Users"), (user.page, user.name), (None, "Posts")],
                   body, f"Posts by {user.name} - {self.board.title}")

    def write_users(self) -> None:
        columns = self.user_columns()
        users = sorted(self.board.users.values(), key=lambda u: (u.board_id is None, u.board_id or 0, -u.id))
        head = "".join(f"<th>{heading}</th>" for heading, _, _ in columns)
        rows = []
        for user in users:
            cells = "".join(f'<td class="{cls}">{cell(user, "")}</td>' if cls else f"<td>{cell(user, '')}</td>"
                            for _, cls, cell in columns)
            rows.append(f"<tr>{cells}</tr>")
        body = (f'<table class="users"><thead><tr>{head}</tr></thead><tbody>\n'
                + "\n".join(rows) + "\n</tbody></table>")
        self.write("users.html", "", [(None, "Users")], body, f"Users - {self.board.title}")


def build_site(db: Database, out: Path, title: str) -> dict[str, int]:
    return SiteBuilder(db, out, title).build()
