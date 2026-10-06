"""BBCode -> HTML for the static site.

Renders the tags the board renders (see content.scan.KNOWN_TAGS) and the "ts:"
sentinels `fix` writes; anything else in brackets stays text, as on the board.
Malformed markup degrades like the board's renderer: a closing tag closes the
tags still open inside it, a closing tag with nothing to close and a tag never
closed stay literal text. Bare URLs are linked, and a line break right after an
opening block tag or around a closing one is dropped (the board ignores those).

What depends on the site (where smileys, attachments, topics and quoted posts
are, and how they're named) is asked to a `Links` object, so boards can change
it; everything presentational is a class name for the stylesheet. The only
inline styles are the post's own colors, sizes and fonts.
"""

import html
import re
import sys
from dataclasses import dataclass, field

from tapascrape.content.quotes import Quote, parse_quotes
from tapascrape.content.scan import KNOWN_TAGS

SENTINELS = {"ts:smiley", "ts:topic", "ts:forum", "ts:attachment", "ts:attachment-image",
             "ts:attachment-link"}
# [align] isn't one of the board's tags, but Invision posts center things with it.
TAGS = KNOWN_TAGS | SENTINELS | {"align"}
ALIGNMENTS = {"left", "right", "center", "justify"}
VOID = {"*", "hr", "ts:smiley", "ts:attachment", "ts:attachment-image"}
BLOCKS = {"quote", "list", "*", "li", "table", "tr", "td", "code", "spoiler", "center", "align", "hr"}
CONTAINERS = {"list", "table", "tr"}  # whitespace directly inside them is layout, not text

# A [code] block is taken whole (its content is literal), like content.quotes does.
TOKEN = re.compile(r"(\[code\b[^\]]*\])(.*?)\[/code\]|\[(/?)(\*|[a-z]+(?::[a-z-]+)?)((?:=|\s)[^\]\n]*)?\]",
                   re.IGNORECASE | re.DOTALL)
ENTITY = re.compile(r"&(?:#\d{1,7}|#x[0-9a-f]{1,6}|[a-z][a-z0-9]{1,31});", re.IGNORECASE)
BARE_URL = re.compile(r"\b(?:(?:https?|ftp)://|www\.)[^\s<>\[\]\"']+", re.IGNORECASE)
TRAILING = ".,;:!?)'\""
COLOR = re.compile(r"#?[0-9a-f]{3}(?:[0-9a-f]{3})?|[a-z]{3,20}", re.IGNORECASE)
FONT = re.compile(r"[a-z0-9 ,'-]{1,40}", re.IGNORECASE)
LEGACY_SIZES = {1: 63, 2: 82, 3: 100, 4: 113, 5: 150, 6: 200, 7: 300}  # <font size=N>, in %
LIST_TYPES = {"1": "1", "a": "a", "A": "A", "i": "i", "I": "I"}
SAFE_URL = re.compile(r"(?:https?|ftp)://|mailto:", re.IGNORECASE)
# Deeper tags stay text (one post nests 210 quotes); rendering recurses a few frames a level.
MAX_DEPTH = 250
RECURSION_LIMIT = 10 * MAX_DEPTH + 1000


@dataclass
class Linked:
    """A file the board had: where the site has it (from the site's root; None: lost) and
    what it's called."""
    href: str | None
    label: str
    image: bool = False


class Links:
    """Where things are. Hrefs are relative to the site's root; None: not on the site."""

    def smiley(self, smiley_id: int) -> Linked | None:
        return None

    def attachment(self, attachment_id: int) -> Linked | None:
        return None

    def topic(self, topic_id: int, attrs: dict[str, str]) -> str | None:
        return None

    def forum(self, forum_id: int) -> str | None:
        return None

    def quoted(self, post_id: int, position: int) -> "Quoted | None":
        """The quoted post of the post's quote at `position`."""
        return None


@dataclass
class Quoted:
    """A quoted post: where it is (from the site's root), who wrote it and when."""
    href: str
    author: str
    date: str


@dataclass
class Node:
    name: str | None                 # None: the root
    attr: str = ""                   # what follows the name in the opening tag
    start: int = 0                   # offset of the opening tag in the text
    open_text: str = ""
    children: list = field(default_factory=list)  # Node or str
    closed: bool = False


def parse(text: str) -> Node:
    root = Node(None, closed=True)
    stack = [root]
    at = 0

    def add_text(piece: str) -> None:
        if piece:
            stack[-1].children.append(piece)

    for m in TOKEN.finditer(text):
        add_text(text[at:m.start()])
        at = m.end()
        if m.group(1):  # [code]...[/code]
            code = Node("code", m.group(1)[5:-1], m.start(), m.group(1), [m.group(2)], True)
            stack[-1].children.append(code)
            continue
        closing, name, attr = m.group(3), m.group(4).lower(), m.group(5) or ""
        if name not in TAGS or name == "code" or (not closing and len(stack) > MAX_DEPTH):
            add_text(m.group(0))
        elif closing:
            if name == "*":
                continue  # [/*]: items end at the next one anyway
            depth = next((i for i in range(len(stack) - 1, 0, -1) if stack[i].name == name), None)
            if depth is None:
                add_text(m.group(0))
                continue
            for node in stack[depth:]:
                node.closed = True
            del stack[depth:]
        else:
            node = Node(name, attr, m.start(), m.group(0), closed=name in VOID)
            stack[-1].children.append(node)
            if name not in VOID:
                stack.append(node)
    add_text(text[at:])
    return root


def escape(text: str) -> str:
    """HTML-escaped text that keeps character references (&nbsp; &#153;) as they are."""
    out, at = [], 0
    for m in ENTITY.finditer(text):
        out.append(html.escape(text[at:m.start()], quote=False))
        out.append(m.group(0))
        at = m.end()
    out.append(html.escape(text[at:], quote=False))
    return "".join(out)


def attribute(value: str) -> str:
    return html.escape(value, quote=True)


def safe_url(url: str) -> str | None:
    url = html.unescape(url.strip())
    if url.lower().startswith("www."):
        url = "http://" + url
    return url if SAFE_URL.match(url) else None


def text_of(node: Node) -> str:
    return "".join(c if isinstance(c, str) else c.open_text + text_of(c) for c in node.children)


def attr_value(attr: str) -> str:
    """The value of [tag=value] (quotes around it removed)."""
    value = attr[1:].strip() if attr.startswith("=") else ""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


def sentinel_attrs(attr: str) -> tuple[int | None, dict[str, str]]:
    """'=996 start=20 old_post=43596' -> (996, {"start": "20", "old_post": "43596"})."""
    head, *rest = attr.lstrip("=").split()
    pairs = dict(item.split("=", 1) for item in rest if "=" in item)
    return (int(head) if head.isdigit() else None), pairs


def font_size(value: str) -> str | None:
    try:
        size = int(value)
    except ValueError:
        return None
    if 1 <= size <= 7:
        return f"{LEGACY_SIZES[size]}%"
    if 8 <= size <= 40:
        return f"{size}px"
    if 50 <= size <= 300:
        return f"{size}%"
    return None


class Renderer:
    def __init__(self, links: Links | None = None):
        self.links = links or Links()

    def render(self, text: str, root: str = "", post_id: int | None = None) -> str:
        """HTML for a post's (or signature's) BBCode; `root` prefixes the site's hrefs."""
        quotes = {q.start: q for q in parse_quotes(text)} if "[quote" in text.lower() else {}
        state = _State(root, post_id, quotes)
        limit = sys.getrecursionlimit()
        sys.setrecursionlimit(max(limit, RECURSION_LIMIT))
        try:
            return self._children(parse(text), state)
        finally:
            sys.setrecursionlimit(limit)

    # -- nodes ---------------------------------------------------------------------------------

    def _children(self, node: Node, state: "_State") -> str:
        out = []
        children = node.children
        for i, child in enumerate(children):
            if isinstance(child, str):
                if node.name in CONTAINERS and not child.strip():
                    continue
                # Line breaks next to blocks are layout.
                if i > 0 and isinstance(children[i - 1], Node) and children[i - 1].name in BLOCKS:
                    child = _drop_first_break(child)
                if i + 1 < len(children) and isinstance(children[i + 1], Node) \
                        and children[i + 1].name in BLOCKS:
                    child = _drop_last_break(child)
                if i == 0 and node.name in BLOCKS:
                    child = _drop_first_break(child)
                if i == len(children) - 1 and node.name in BLOCKS:
                    child = _drop_last_break(child)
                out.append(self._text(child))
            else:
                out.append(self._node(child, state))
        return "".join(out)

    def _text(self, text: str) -> str:
        out, at = [], 0
        for m in BARE_URL.finditer(text):
            url = m.group(0).rstrip(TRAILING)
            href = safe_url(url)
            if href is None:
                continue
            out.append(escape(text[at:m.start()]))
            out.append(f'<a href="{attribute(href)}">{escape(url)}</a>')
            at = m.start() + len(url)
        out.append(escape(text[at:]))
        return "".join(out).replace("\r\n", "\n").replace("\n", "<br>\n")

    def _literal(self, node: Node, state: "_State") -> str:
        return self._text(node.open_text) + self._children(node, state)

    def _node(self, node: Node, state: "_State") -> str:
        if not node.closed:
            return self._literal(node, state)
        method = getattr(self, "tag_" + node.name.replace(":", "_").replace("-", "_").replace("*", "item"))
        rendered = method(node, state)
        return rendered if rendered is not None else self._literal(node, state) + self._close(node)

    @staticmethod
    def _close(node: Node) -> str:
        return "" if node.name in VOID else escape(f"[/{node.name}]")

    def _wrap(self, tag: str, node: Node, state: "_State", cls: str | None = None) -> str:
        cls_attr = f' class="{cls}"' if cls else ""
        return f"<{tag}{cls_attr}>{self._children(node, state)}</{tag}>"

    # -- tags ----------------------------------------------------------------------------------

    def tag_b(self, node, state):
        return self._wrap("b", node, state)

    def tag_i(self, node, state):
        return self._wrap("i", node, state)

    def tag_u(self, node, state):
        return self._wrap("u", node, state)

    def tag_s(self, node, state):
        return self._wrap("s", node, state)

    def tag_sup(self, node, state):
        return self._wrap("sup", node, state)

    def tag_sub(self, node, state):
        return self._wrap("sub", node, state)

    def tag_center(self, node, state):
        return self._wrap("div", node, state, "align-center")

    def tag_align(self, node, state):
        side = attr_value(node.attr).strip().lower()
        if side not in ALIGNMENTS:
            return None
        return self._wrap("div", node, state, f"align-{side}")

    def tag_hr(self, node, state):
        return "<hr>"

    def tag_table(self, node, state):
        return self._wrap("table", node, state, "bb-table")

    def tag_tr(self, node, state):
        return self._wrap("tr", node, state)

    def tag_td(self, node, state):
        return self._wrap("td", node, state)

    def tag_color(self, node, state):
        value = attr_value(node.attr)
        if not COLOR.fullmatch(value):
            return None
        return f'<span style="color:{value}">{self._children(node, state)}</span>'

    def tag_size(self, node, state):
        size = font_size(attr_value(node.attr))
        if size is None:
            return None
        return f'<span style="font-size:{size}">{self._children(node, state)}</span>'

    def tag_font(self, node, state):
        value = attr_value(node.attr)
        if not FONT.fullmatch(value):
            return None
        return f'<span style="font-family:{attribute(value)}">{self._children(node, state)}</span>'

    def tag_url(self, node, state):
        target = attr_value(node.attr) or text_of(node)
        href = safe_url(target)
        if href is None:
            return None
        inner = self._children(node, state) if node.attr else ""
        return f'<a href="{attribute(href)}">{inner if inner.strip() else escape(target)}</a>'

    def tag_email(self, node, state):
        address = (attr_value(node.attr) or text_of(node)).strip()
        if "@" not in address or any(c in address for c in " <>\"'"):
            return None
        inner = self._children(node, state) if node.attr else escape(address)
        return f'<a href="mailto:{attribute(address)}">{inner}</a>'

    def tag_img(self, node, state):
        src = safe_url(text_of(node))
        if src is None:
            return None
        return f'<img class="bb-img" src="{attribute(src)}" alt="" loading="lazy">'

    def tag_code(self, node, state):
        return f'<pre class="code">{escape(text_of(node).strip(chr(10)))}</pre>'

    def tag_spoiler(self, node, state):
        title = attr_value(node.attr)
        label = "Spoiler" + (f": {escape(title)}" if title.strip() else "")
        return (f'<details class="spoiler"><summary>{label}</summary>'
                f'{self._children(node, state)}</details>')

    def tag_list(self, node, state):
        kind = LIST_TYPES.get(attr_value(node.attr))
        tag = "ol" if kind else "ul"
        type_attr = f' type="{kind}"' if kind and kind != "1" else ""
        items, current = [], None
        for child in node.children:
            if isinstance(child, Node) and child.name == "*":
                current = []
                items.append(current)
            elif current is not None:
                current.append(child)
            elif not isinstance(child, str) or child.strip():
                items.append([child])  # before the first [*]
                current = items[-1]
        body = "".join(f"<li>{self._children(Node('li', children=item, closed=True), state)}</li>"
                       for item in items)
        return f"<{tag}{type_attr}>{body}</{tag}>"

    def tag_item(self, node, state):
        return "&bull; "  # [*] outside a list

    def tag_quote(self, node, state):
        quote: Quote | None = state.quotes.get(node.start)
        found = (self.links.quoted(state.post_id, quote.position)
                 if quote is not None and state.post_id is not None else None)
        # The tag's own attribution, as written; what it lacks, from the quoted post.
        author = (quote.author if quote else None) or (found.author if found else None)
        date = (quote.date if quote else None) or (found.date if found else None)
        head = "Quote"
        if author or date:
            head += ": " + ", ".join(escape(part) for part in (author, date) if part)
        elif node.attr:
            head += ": " + escape(attr_value(node.attr) or node.attr.strip())
        if found is not None:
            head = f'<a href="{attribute(state.root + found.href)}">{head}</a>'
        return (f'<blockquote class="quote"><div class="quote-head">{head}</div>'
                f'{self._children(node, state)}</blockquote>')

    # -- sentinels -----------------------------------------------------------------------------

    def tag_ts_smiley(self, node, state):
        smiley_id, _ = sentinel_attrs(node.attr)
        found = self.links.smiley(smiley_id) if smiley_id is not None else None
        if found is None or found.href is None:
            name = f":{found.label}:" if found and found.label else "[smiley]"
            return f'<span class="smiley-missing">{escape(name)}</span>'
        return (f'<img class="smiley" src="{attribute(state.root + found.href)}" '
                f'alt="{attribute(found.label)}" title="{attribute(found.label)}">')

    def _attachment(self, node, state) -> Linked | None:
        attachment_id, _ = sentinel_attrs(node.attr)
        return self.links.attachment(attachment_id) if attachment_id is not None else None

    def tag_ts_attachment(self, node, state):
        found = self._attachment(node, state)
        if found is None or found.href is None:
            name = f": {escape(found.label)}" if found and found.label else ""
            return f'<div class="attachment attachment-missing">Attachment (lost){name}</div>'
        href = attribute(state.root + found.href)
        if found.image:
            return f'<div class="attachment"><img class="bb-img" src="{href}" alt="{attribute(found.label)}"></div>'
        return f'<div class="attachment">Attachment: <a href="{href}">{escape(found.label)}</a></div>'

    def tag_ts_attachment_image(self, node, state):
        found = self._attachment(node, state)
        if found is None or found.href is None:
            name = f": {escape(found.label)}" if found and found.label else ""
            return f'<span class="attachment-missing">[image lost{name}]</span>'
        return (f'<img class="bb-img" src="{attribute(state.root + found.href)}" '
                f'alt="{attribute(found.label)}" loading="lazy">')

    def tag_ts_attachment_link(self, node, state):
        found = self._attachment(node, state)
        if found is None or found.href is None:
            return f'<span class="attachment-missing">{self._children(node, state)}</span>'
        return f'<a href="{attribute(state.root + found.href)}">{self._children(node, state)}</a>'

    def tag_ts_topic(self, node, state):
        topic_id, attrs = sentinel_attrs(node.attr)
        href = self.links.topic(topic_id, attrs) if topic_id is not None else None
        if href is None:
            return self._children(node, state)
        return f'<a href="{attribute(state.root + href)}">{self._children(node, state)}</a>'

    def tag_ts_forum(self, node, state):
        forum_id, _ = sentinel_attrs(node.attr)
        href = self.links.forum(forum_id) if forum_id is not None else None
        if href is None:
            return self._children(node, state)
        return f'<a href="{attribute(state.root + href)}">{self._children(node, state)}</a>'


@dataclass
class _State:
    root: str
    post_id: int | None
    quotes: dict[int, Quote]


def _drop_first_break(text: str) -> str:
    if text.startswith("\r\n"):
        return text[2:]
    return text[1:] if text.startswith("\n") else text


def _drop_last_break(text: str) -> str:
    if text.endswith("\r\n"):
        return text[:-2]
    return text[:-1] if text.endswith("\n") else text
