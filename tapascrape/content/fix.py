"""Repair migration leftovers in post sources: posts.source -> posts.source_fixed.

`source` is never modified; `source_fixed` is recomputed from it on every run,
so fixes can be changed, added or re-run freely. Each fix is a small named,
deterministic text transformation; they run in FIXES order.

Fixes that work on tag pairs (tables, sizes) rewrite the innermost pair first
and set aside, untouched, the pairs that don't have the expected shape, so
nested and malformed markup is handled without guessing.

Some fixes write sentinels: tags in the reserved "ts:" namespace, which the
board never accepted (tag names can't contain ":"), standing for things only a
later stage can resolve:

    [ts:smiley=12]                              a smiley of the `smilies` table
    [ts:topic=996 start=20 old_post=43596]text[/ts:topic]   link to a topic
    [ts:forum=39]text[/ts:forum]                link to a forum

Sentinels are never written inside [code] blocks, nor in posts whose source
already contains "[ts:" (so they can't be confused with a post's own text).
"""

import logging
import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlsplit

from tapascrape.content.scan import iter_signature_sources, iter_sources
from tapascrape.content.smilies import SMILEY_IMG, normalize_url, smiley_ids
from tapascrape.crawl.progress import Progress
from tapascrape.db.base import Database

log = logging.getLogger(__name__)

SENTINEL = re.compile(r"\[/?ts:", re.IGNORECASE)


@dataclass
class FixContext:
    """What fixes may need from the database."""
    smilies: dict[str, int] = field(default_factory=dict)  # normalized URL -> smiley id
    topics: set[int] | None = None  # existing topic ids; None accepts any
    forums: set[int] | None = None


def load_context(db: Database) -> FixContext:
    return FixContext(smilies=smiley_ids(db),
                      topics={r[0] for r in db.query("SELECT id FROM topics")},
                      forums={r[0] for r in db.query("SELECT id FROM forums")})


@dataclass(frozen=True)
class Fix:
    name: str
    description: str
    apply: Callable[[str, FixContext], str]
    sentinel: bool = False  # writes "ts:" tags


class Holder:
    """Sets parts of a text aside behind private-use characters, and puts them back."""

    def __init__(self, open_char: str, close_char: str):
        self.open, self.close = open_char, close_char
        self.marker = re.compile(f"{open_char}(\\d+){close_char}")
        self.held: list[str] = []

    def usable(self, text: str) -> bool:
        return self.open not in text and self.close not in text

    def hold(self, piece: str) -> str:
        self.held.append(piece)
        return f"{self.open}{len(self.held) - 1}{self.close}"

    def restore(self, text: str) -> str:
        while self.marker.search(text):
            text = self.marker.sub(lambda m: self.held[int(m.group(1))], text)
        return text


def rewrite_pairs(text: str, pair: re.Pattern, rewrite: Callable[[re.Match], str | None]) -> str:
    """Rewrite innermost-first every match of `pair` (an open tag, content without
    nested pairs, close tag); `rewrite` returns None to leave a pair as it is."""
    holder = Holder("", "")
    if not holder.usable(text):
        return text  # can't set pairs aside safely

    def replace(match: re.Match) -> str:
        new = rewrite(match)
        return new if new is not None else holder.hold(match.group(0))

    while True:
        text, n = pair.subn(replace, text)
        if not n:
            break
    return holder.restore(text)


CODE_BLOCK = re.compile(r"\[code\b[^\]]*\].*?\[/code\]", re.IGNORECASE | re.DOTALL)


def outside_code(text: str, fix: Callable[[str], str]) -> str:
    """Apply `fix` to everything but [code] blocks, whose content is literal."""
    holder = Holder("", "")
    if not holder.usable(text):
        return text
    masked = CODE_BLOCK.sub(lambda m: holder.hold(m.group(0)), text)
    return holder.restore(fix(masked))


# -- [table] quotes and code blocks ---------------------------------------

TABLE = re.compile(r"\[table\]((?:(?!\[/?table\]).)*)\[/table\]", re.DOTALL)
TABLE_BLOCK = re.compile(r"\[tr\]\[td\]\[b\](QUOTE|CODE)\[/b\]([^\[\]]*)\[/td\]\[/tr\]"
                         r"\[tr\]\[td\](.*)\[/td\]\[/tr\]", re.DOTALL)
# "(author @ Nov 4 2005, 07:37 PM)", "(author @ October 03, 2006 05:34 pm)",
# "(author @ May 2 2004 @ 03:59 AM)"
QUOTE_DATE = re.compile(r"[A-Z][a-z]+ \d{1,2},? \d{4}(?:,|\s+@)?\s+\d{1,2}:\d{2} ?[AaPp][Mm]")


def quote_attr(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def quote_open(header: str) -> str:
    """[quote...] for a table header such as " (author @ date)"."""
    header = header.strip()
    if header.startswith("(") and header.endswith(")"):
        header = header[1:-1].strip()
    if not header:
        return "[quote]"
    author, sep, date = header.partition(" @ ")
    if sep and author.strip() and QUOTE_DATE.fullmatch(date.strip()):
        return f"[quote={quote_attr(author.strip())} date={quote_attr(date.strip())}]"
    return f"[quote={quote_attr(header)}]"


def _table_block(match: re.Match) -> str | None:
    block = TABLE_BLOCK.fullmatch(match.group(1))
    if block is None:
        return None
    kind, header, body = block.groups()
    if "[/td]" in body or "[tr]" in body:
        return None  # more cells than a quote/code block has
    if kind == "CODE":
        if header.strip() or re.search(r"\[/code\]", body, re.IGNORECASE):
            return None
        return f"[code]{body}[/code]"
    return f"{quote_open(header)}{body}[/quote]"


def fix_table_blocks(text: str, ctx: FixContext) -> str:
    if "[b]QUOTE[/b]" not in text and "[b]CODE[/b]" not in text:
        return text
    return rewrite_pairs(text, TABLE, _table_block)


# -- simple ones -----------------------------------------------------------

ATTRIBUTE_LEAK = re.compile(r"\[(\w+)=([^\]\n]*?)'>\]")
QUOTED_URL = re.compile(r"\[url='([^'\[\]\n]*)'\]", re.IGNORECASE)
QUOTED_IMG = re.compile(r"\[img\]'([^'\[\]\n]*)'\[/img\]", re.IGNORECASE)
SIZE = re.compile(r"\[size=([^\]\n]*)\]((?:(?!\[/?size[=\]]).)*)\[/size\]",
                  re.IGNORECASE | re.DOTALL)


def fix_attribute_leak(text: str, ctx: FixContext) -> str:
    return ATTRIBUTE_LEAK.sub(r"[\1=\2]", text)


def fix_quoted_url(text: str, ctx: FixContext) -> str:
    return QUOTED_URL.sub(r"[url=\1]", text)


def fix_quoted_img(text: str, ctx: FixContext) -> str:
    return QUOTED_IMG.sub(r"[img]\1[/img]", text)


def fix_size_100(text: str, ctx: FixContext) -> str:
    if "=100]" not in text:
        return text
    return rewrite_pairs(text, SIZE, lambda m: m.group(2) if m.group(1).strip() == "100" else None)


# -- sentinels ---------------------------------------------------------------

def fix_smilies(text: str, ctx: FixContext) -> str:
    if not ctx.smilies:
        return text

    def replace(match: re.Match) -> str:
        smiley_id = ctx.smilies.get(normalize_url(match.group(1)))
        return f"[ts:smiley={smiley_id}]" if smiley_id is not None else match.group(0)

    return outside_code(text, lambda t: SMILEY_IMG.sub(replace, t))


OLD_BOARD_URL = r"https?://(?:www\.)?metanet\.(?:2\.)?forumer\.com/(?:index\.php)?\?[^\s\[\]<>\"']*"
OLD_LINK = re.compile(rf"\[url=({OLD_BOARD_URL})\](.*?)\[/url\]", re.IGNORECASE | re.DOTALL)
OLD_AUTOLINK = re.compile(rf"\[url\]({OLD_BOARD_URL})\[/url\]", re.IGNORECASE)
OLD_BARE = re.compile(OLD_BOARD_URL, re.IGNORECASE)
# Pairs whose content must not be touched when looking for bare URLs.
LINKISH_PAIR = re.compile(r"\[(url|img|ts:topic|ts:forum)\b[^\]]*\].*?\[/\1\]",
                          re.IGNORECASE | re.DOTALL)
TRAILING_PUNCTUATION = ".,;:!?)"


def old_link_tags(url: str, ctx: FixContext) -> tuple[str, str] | None:
    """(open, close) sentinel tags for a link to the old board, or None to leave it."""
    parts = urlsplit(url.replace("&amp;", "&"))
    query = {k.lower(): v for k, v in parse_qsl(parts.query, keep_blank_values=True)}
    act = query.get("act", "").lower()
    topic = query.get("showtopic") or (query.get("t") if act == "st" else None)
    forum = query.get("showforum") or (query.get("f") if act == "sf" else None)
    if topic and topic.isdigit():
        topic_id = int(topic)
        if ctx.topics is not None and topic_id not in ctx.topics:
            return None
        attrs = f"[ts:topic={topic_id}"
        if (start := query.get("st", "")).isdigit() and int(start):
            attrs += f" start={int(start)}"
        old_post = query.get("p") if query.get("view", "").lower() == "findpost" else None
        if not old_post and (entry := re.fullmatch(r"entry(\d+)", parts.fragment)):
            old_post = entry.group(1)
        if old_post and old_post.isdigit():
            attrs += f" old_post={int(old_post)}"
        return attrs + "]", "[/ts:topic]"
    if forum and forum.isdigit() and not topic:
        forum_id = int(forum)
        if ctx.forums is not None and forum_id not in ctx.forums:
            return None
        return f"[ts:forum={forum_id}]", "[/ts:forum]"
    return None


def fix_old_links(text: str, ctx: FixContext) -> str:
    if "forumer.com" not in text.lower():
        return text

    def link(match: re.Match) -> str:
        tags = old_link_tags(match.group(1), ctx)
        return f"{tags[0]}{match.group(2)}{tags[1]}" if tags else match.group(0)

    def autolink(match: re.Match) -> str:
        tags = old_link_tags(match.group(1), ctx)
        return f"{tags[0]}{match.group(1)}{tags[1]}" if tags else match.group(0)

    def bare(match: re.Match) -> str:
        url = match.group(0)
        trimmed = url.rstrip(TRAILING_PUNCTUATION)
        tags = old_link_tags(trimmed, ctx)
        return f"{tags[0]}{trimmed}{tags[1]}{url[len(trimmed):]}" if tags else url

    def links(part: str) -> str:
        part = OLD_AUTOLINK.sub(autolink, OLD_LINK.sub(link, part))
        holder = Holder("", "")
        if not holder.usable(part):
            return part
        masked = LINKISH_PAIR.sub(lambda m: holder.hold(m.group(0)), part)
        return holder.restore(OLD_BARE.sub(bare, masked))

    return outside_code(text, links)


FIXES = [
    Fix("attribute-leak", "[color=RED'>] -> [color=RED]", fix_attribute_leak),
    Fix("quoted-url", "[url='x'] -> [url=x]", fix_quoted_url),
    Fix("quoted-img", "[img]'x'[/img] -> [img]x[/img]", fix_quoted_img),
    Fix("size-100", "[size=100]x[/size] -> x", fix_size_100),
    Fix("table-blocks", "quote/code tables -> [quote=\"author\" date=\"...\"] / [code]",
        fix_table_blocks),
    Fix("smilies", "[img]<dead smiley host>[/img] -> [ts:smiley=ID]", fix_smilies, sentinel=True),
    Fix("old-links", "old forumer topic/forum links -> [ts:topic=N]...[/ts:topic]",
        fix_old_links, sentinel=True),
]
FIXES_BY_NAME = {f.name: f for f in FIXES}


def fix_source(text: str, fixes: Iterable[Fix] = FIXES,
               ctx: FixContext | None = None) -> tuple[str, list[str]]:
    """(fixed text, names of the fixes that changed something)."""
    ctx = ctx if ctx is not None else FixContext()
    reserved = SENTINEL.search(text) is not None
    applied = []
    for fix in fixes:
        if fix.sentinel and reserved:
            continue
        fixed = fix.apply(text, ctx)
        if fixed != text:
            applied.append(fix.name)
            text = fixed
    return text, applied


def fix_posts(db: Database, fixes: Iterable[Fix] = FIXES, batch: int = 2000) -> Counter:
    """Recompute posts.source_fixed for every post with a source; posts changed per fix."""
    fixes = list(fixes)
    ctx = load_context(db)
    if not ctx.smilies and any(f.name == "smilies" for f in fixes):
        log.warning("fix: the smilies table is empty; run `tapascrape smilies` first "
                    "to replace smiley images")
    (total,), = db.query("SELECT COUNT(*) FROM posts WHERE source IS NOT NULL")
    progress = Progress("fix", total)
    changed: Counter = Counter()
    pending: list[dict] = []

    def flush() -> None:
        with db.transaction():
            db.update_many("posts", pending)
        progress.advance(len(pending))
        pending.clear()

    for post_id, source in iter_sources(db, batch):
        fixed, applied = fix_source(source, fixes, ctx)
        changed.update(applied)
        changed["(any)"] += bool(applied)
        pending.append({"id": post_id, "source_fixed": fixed})
        if len(pending) >= batch:
            flush()
    if pending:
        flush()
    log.info("fix: %d of %d posts changed", changed["(any)"], total)
    return changed


def fix_signatures(db: Database, fixes: Iterable[Fix] = FIXES) -> Counter:
    """users.signature (HTML) -> signature_source (rebuilt BBCode) -> signature_fixed."""
    fixes = list(fixes)
    ctx = load_context(db)
    changed: Counter = Counter()
    rows = []
    for user_id, source in iter_signature_sources(db):
        fixed, applied = fix_source(source, fixes, ctx)
        changed.update(applied)
        changed["(any)"] += bool(applied)
        rows.append({"id": user_id, "signature_source": source, "signature_fixed": fixed})
    with db.transaction():
        db.update_many("users", rows)
    log.info("fix: %d of %d signatures changed", changed["(any)"], len(rows))
    return changed
