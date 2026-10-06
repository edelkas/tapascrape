"""A static HTML site to browse an archived board: `tapascrape site`.

    index.html          the board's forum tree
    f/<id>.html         a forum: its subforum tree, then its topics
    t/<id>.html         a topic: every post, oldest first (anchors #p<post id>)
    u/<id>.html         a user's profile
    files/...           smileys, attachments and avatars stored in the database
    style.css           every bit of styling; edit it freely

Plain HTML and CSS, no JavaScript, one page per forum/topic/user, no pagination.
Everything is plain text except post bodies and signatures, rendered from their
BBCode (see render.py). Quotes link to the post they quote (the quotes table).

SiteBuilder loads the board into a small model, then writes it; boards with
more to say (other names, extra profile fields, lost topics) subclass it and
override `load` or the small hooks (user_name, user_fields, post_text...).
"""

import html
import logging
import re
import shutil
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
    id: int
    topic_id: int
    user_id: int | None
    timestamp: datetime | None
    index: int


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
    id: int
    name: str
    rank: str | None
    joined_at: datetime | None
    last_active_at: datetime | None
    post_count: int | None
    signature: str | None            # BBCode
    avatar: str | None = None        # href from the site's root
    first_post_id: int | None = None
    last_post_id: int | None = None
    groups: list[str] = field(default_factory=list)


@dataclass
class Board:
    title: str
    forums: dict[int, Forum] = field(default_factory=dict)
    roots: list[Forum] = field(default_factory=list)
    topics: dict[int, Topic] = field(default_factory=dict)
    users: dict[int, User] = field(default_factory=dict)
    posts: dict[int, PostRef] = field(default_factory=dict)
    quotes: dict[tuple[int, int], int] = field(default_factory=dict)  # (post, position) -> quoted post
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
        return Quoted(f"t/{post.topic_id}.html#p{post.id}", self.site.user_name(post.user_id),
                      iso(post.timestamp))


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
        for sub in ("f", "t", "u", "files/smilies", "files/attachments", "files/avatars"):
            (self.out / sub).mkdir(parents=True, exist_ok=True)
        self.write_files()
        shutil.copyfile(resources.files("tapascrape.site").joinpath("style.css"), self.out / "style.css")
        self.write("index.html", "", [], self.forum_table(self.board.roots, ""), self.board.title)
        for forum in self.board.forums.values():
            self.write_forum(forum)
        self.write_topics()
        for user in self.board.users.values():
            self.write_user(user)
        return {"forums": len(self.board.forums), "topics": len(self.board.topics),
                "users": len(self.board.users), "posts": len(self.board.posts)}

    # -- loading -------------------------------------------------------------------------------

    def load(self) -> None:
        board, q = self.board, self.db.quote_ident
        for row in self.db.query("SELECT id, parent_id, name, description, post_count, view_count, "
                                 "last_post_id FROM forums"):
            board.forums[row[0]] = Forum(*row[:3], row[3] or "", *row[4:])
        for forum in board.forums.values():
            parent = board.forums.get(forum.parent_id)
            (parent.children if parent else board.roots).append(forum)
        for row in self.db.query("SELECT id, forum_id, user_id, name, stickied, locked, created_at, "
                                 "post_count, view_count, last_post_id FROM topics"):
            topic = Topic(*row[:4], bool(row[4]), bool(row[5]), as_datetime(row[6]), *row[7:])
            board.topics[topic.id] = topic
            if topic.forum_id in board.forums:
                board.forums[topic.forum_id].topics.append(topic)
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
        self.first_and_last_posts()
        board.quotes = {(post_id, position): quoted for post_id, position, quoted in self.db.query(
            "SELECT post_id, position, quoted_post_id FROM quotes WHERE quoted_post_id IS NOT NULL")}

    def first_and_last_posts(self) -> None:
        for post in sorted(self.board.posts.values(), key=self.chronological):
            user = self.board.users.get(post.user_id)
            if user is not None:
                user.first_post_id = user.first_post_id or post.id
                user.last_post_id = post.id

    @staticmethod
    def chronological(post: PostRef) -> tuple:
        return (post.timestamp is None, post.timestamp or datetime.min, post.index, post.id)

    # -- files ---------------------------------------------------------------------------------

    def write_files(self) -> None:
        """Smileys, attachments and avatars stored in the database -> files/."""
        board = self.board
        for smiley_id, name, data in self.db.query("SELECT id, name, data FROM smilies"):
            href = None
            if data is not None:
                href = f"files/smilies/{smiley_id}{EXTENSIONS.get(image_type(data), '')}"
                (self.out / href).write_bytes(data)
            board.smilies[smiley_id] = Linked(href, name or "", True)
        for attachment_id, name, content_type, data in self.db.query(
                "SELECT id, name, content_type, data FROM attachments"):
            name = name or f"attachment{EXTENSIONS.get(content_type or '', '')}"
            href = None
            if data is not None:
                href = f"files/attachments/{attachment_id}-{UNSAFE_NAME.sub('_', name)}"
                (self.out / href).write_bytes(data)
            board.attachments[attachment_id] = Linked(href, name, data is not None and image_type(data) is not None)
        for user_id, data in self.db.query(
                "SELECT u.id, a.data FROM users u JOIN avatars a ON a.id = u.avatar_id"):
            user = board.users.get(user_id)
            if user is not None:
                user.avatar = f"files/avatars/{user_id}{EXTENSIONS.get(image_type(data), '')}"
                (self.out / user.avatar).write_bytes(data)

    # -- hooks for boards that know more ---------------------------------------------------------

    def user_name(self, user_id: int | None) -> str:
        user = self.board.users.get(user_id)
        return user.name if user else "Guest" if not user_id else f"user {user_id}"

    def post_author(self, post: PostRef, root: str) -> str:
        """HTML naming a post's author (a link to their page, if they have one)."""
        return self.user_link(post.user_id, root)

    def user_fields(self, user: User, root: str) -> list[tuple[str, str]]:
        """(label, HTML) rows of a user's page, signature aside."""
        rows = [("Name", escape(user.name)), ("Rank", plain(user.rank)),
                ("Groups", plain(", ".join(user.groups))), ("Joined", iso(user.joined_at)),
                ("Last active", iso(user.last_active_at)), ("Posts", number(user.post_count))]
        for label, post_id in (("First post", user.first_post_id), ("Last post", user.last_post_id)):
            post = self.board.posts.get(post_id) if post_id else None
            topic = self.board.topics.get(post.topic_id) if post else None
            if post is not None:
                name = escape(topic.name) if topic else f"topic {post.topic_id}"
                rows.append((label, f'{iso(post.timestamp)} <a href="{root}t/{post.topic_id}.html#p{post.id}">'
                                    f"{name}</a>"))
        return rows

    def post_text(self, source_fixed: str | None, source: str | None, content: str | None) -> str:
        if source_fixed is not None:
            return source_fixed
        if source is not None:
            return source
        return html_to_bbcode(content) if content else ""

    # -- pieces --------------------------------------------------------------------------------

    def user_link(self, user_id: int | None, root: str) -> str:
        name = escape(self.user_name(user_id))
        return f'<a href="{root}u/{user_id}.html">{name}</a>' if user_id in self.board.users else name

    def last_post_cells(self, post_id: int | None, root: str) -> str:
        post = self.board.posts.get(post_id) if post_id else None
        if post is None:
            return "<td></td><td></td>"
        return (f'<td>{self.user_link(post.user_id, root)}</td>'
                f'<td class="date"><a href="{root}t/{post.topic_id}.html#p{post.id}">{iso(post.timestamp)}</a></td>')

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
        ordered = sorted(topics, key=lambda t: (t.stickied, self.last_time(t.last_post_id), t.id),
                         reverse=True)
        rows = []
        for topic in ordered:
            flags = ("S" if topic.stickied else "&nbsp;") + ("L" if topic.locked else "&nbsp;")
            rows.append(
                f'<tr id="t{topic.id}"><td class="id"><a href="{root}f/{topic.forum_id}.html#t{topic.id}">'
                f'{topic.id}</a></td><td class="flags">{flags}</td>'
                f'<td class="name"><a href="{root}t/{topic.id}.html">{plain(topic.name)}</a></td>'
                f'<td>{self.user_link(topic.user_id, root)}</td><td class="date">{iso(topic.created_at)}</td>'
                f'<td class="count">{number(topic.post_count)}</td><td class="count">{number(topic.view_count)}</td>'
                f'{self.last_post_cells(topic.last_post_id, root)}</tr>')
        return ('<table class="topics"><thead><tr><th>ID</th><th>Flags</th><th>Topic</th><th>Started by</th>'
                '<th>Started</th><th>Posts</th><th>Views</th><th>Last post by</th><th>Last post</th>'
                '</tr></thead><tbody>\n' + "\n".join(rows) + "\n</tbody></table>")

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
                f'</div><nav class="crumbs">{trail}</nav></header>\n<main>\n{body}\n</main>\n</body>\n</html>\n')
        (self.out / path).write_text(page, encoding="utf-8")

    def write_forum(self, forum: Forum) -> None:
        root = "../"
        body = self.forum_table(forum.children, root) + self.topic_table(forum.topics, root)
        crumbs = self.crumbs(forum.id)
        crumbs[-1] = (None, crumbs[-1][1])  # the page itself
        self.write(f"f/{forum.id}.html", root, crumbs, body or '<p class="empty">Nothing here.</p>',
                   f"{forum.name} - {self.board.title}")

    def write_topics(self) -> None:
        by_topic: dict[int, list[PostRef]] = {topic_id: [] for topic_id in self.board.topics}
        for post in self.board.posts.values():
            by_topic.setdefault(post.topic_id, []).append(post)
        progress = Progress("site: topics", len(by_topic))
        for topic_id, posts in by_topic.items():
            self.write_topic(topic_id, sorted(posts, key=self.chronological))
            progress.advance()

    def write_topic(self, topic_id: int, posts: list[PostRef]) -> None:
        root, q = "../", self.db.quote_ident
        texts = {row[0]: self.post_text(*row[1:]) for row in self.db.query(
            "SELECT id, source_fixed, source, CASE WHEN source IS NULL AND source_fixed IS NULL "
            f"THEN content END FROM posts WHERE topic_id = ?", (topic_id,))}
        out = []
        for post in posts:
            body = self.renderer.render(texts.get(post.id, ""), root, post.id)
            out.append(f'<div class="post" id="p{post.id}"><div class="post-head">'
                       f'<a class="id" href="{root}t/{topic_id}.html#p{post.id}">#{post.id}</a> '
                       f'<span class="index">{post.index}</span> '
                       f'<span class="date">{iso(post.timestamp)}</span> '
                       f'<span class="author">{self.post_author(post, root)}</span></div>'
                       f'<div class="post-body">{body}</div></div>')
        topic = self.board.topics.get(topic_id)
        name = topic.name if topic else f"Topic {topic_id}"
        crumbs = self.crumbs(topic.forum_id if topic else None) + [(None, name)]
        self.write(f"t/{topic_id}.html", root, crumbs, "\n".join(out), f"{name} - {self.board.title}")

    def write_user(self, user: User) -> None:
        root = "../"
        rows = "".join(f'<tr><th>{label}</th><td>{value}</td></tr>' for label, value in self.user_fields(user, root))
        avatar = (f'<img class="avatar" src="{root}{user.avatar}" alt="">' if user.avatar else "")
        signature = (f'<div class="signature">{self.renderer.render(user.signature, root)}</div>'
                     if user.signature and user.signature.strip() else "")
        body = f'<div class="user">{avatar}<table class="profile">{rows}</table></div>{signature}'
        self.write(f"u/{user.id}.html", root, [(None, user.name)], body, f"{user.name} - {self.board.title}")


def build_site(db: Database, out: Path, title: str) -> dict[str, int]:
    return SiteBuilder(db, out, title).build()
