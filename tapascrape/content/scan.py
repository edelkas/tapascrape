"""Read-only report of markup problems in post sources.

Most of them are leftovers of the board's migrations (Yuku/Invision ->
forumer -> Tapatalk): HTML-to-BBCode conversions that broke on single-quoted
attributes, quotes turned into tables, smileys pointing at dead hosts, and so
on. Each rule is a regex with a description; the report counts matching posts
and shows a few examples, to decide which fixes are worth applying.

Besides the rules, known tags are checked for balance (open vs. close counts
per post), and bracketed words the board doesn't render as tags are listed:
many are plain text ("[sarcasm]"), some may be lost markup.
"""

import re
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TextIO

from tapascrape.db.base import Database
from tapascrape.parse.bbcode import html_to_bbcode


@dataclass(frozen=True)
class Rule:
    name: str
    description: str
    pattern: re.Pattern


def rule(name: str, description: str, pattern: str, flags: int = re.IGNORECASE) -> Rule:
    return Rule(name, description, re.compile(pattern, flags))


RULES = [
    rule("table-quote", "quote converted to a table: [table][tr][td][b]QUOTE[/b] (author)...",
         r"\[table\]\[tr\]\[td\]\[b\]QUOTE\[/b\]", 0),
    rule("table-code", "code block converted to a table: [table][tr][td][b]CODE[/b]...",
         r"\[table\]\[tr\]\[td\]\[b\]CODE\[/b\]", 0),
    rule("attribute-leak", "HTML leftover in a tag attribute, e.g. [color=RED'>]",
         r"\[(\w+)=[^\]\n]*'>\]"),
    rule("quoted-url", "URL wrapped in single quotes: [url='...']", r"\[url='[^'\]\n]*'\]"),
    rule("quoted-img", "image URL wrapped in single quotes: [img]'...'[/img]",
         r"\[img\]'[^'\[\n]*'\[/img\]"),
    rule("yuku-smiley", "smiley image on Yuku's (dead) static host",
         r"\[img\]https?://static\.yuku\.com/domain/bypass/images/[^\[]*\[/img\]"),
    rule("forumer-emoticon", "smiley image on forumer's (dead) host",
         r"\[img\]'?https?://2\.forumer\.com/html/emoticons/[^\[]*\[/img\]"),
    rule("ipb-emoticon", "Invision smiley left as text: (IMG:style_emoticons/...)",
         r"\(IMG:style_emoticons/[^)]*\)"),
    rule("ipb-html", "raw Invision HTML/comments, e.g. <!--QuoteBegin-->",
         r"<!--(?:Quote|QuoteE|Code|EndCode|c1|ec1|c2|ec2|emo&|endemo|sizeo|sizec|colorc?o?)"),
    rule("html-br", "HTML line break in the source: <br>", r"<br\s*/?>"),
    rule("html-entity", "HTML entity in the source, e.g. &nbsp; &#153;", r"&(?:#\d+|[a-z]+);"),
    rule("size-100", "[size=100], which changes nothing", r"\[size=100\]"),
    rule("quote-with-date", "Invision-style quote attribution, e.g. [QUOTE=name @ date]",
         r"\[quote=[^\]\n]*(?:@|,)\s*\w+ \d+,? \d{4}"),
    rule("odd-spoiler-title", "spoiler with an empty, '_' or markup title",
         r"\[spoiler=(?:_?\]|\[)"),
    rule("attachment-link", "link to a migrated attachment (relative /attach/ URL)",
         r"\[url=/attach/[^\]]*\]"),
    rule("old-forum-link", "link to a topic of the old forumer board",
         r"metanet\.2\.forumer\.com/index\.php\?showtopic=\d+"),
]
RULES_BY_NAME = {r.name: r for r in RULES}

# Tags the board renders (anything else in brackets shows as plain text).
KNOWN_TAGS = {"b", "i", "u", "s", "url", "img", "color", "size", "font", "quote", "spoiler",
              "code", "list", "*", "sup", "sub", "hr", "table", "tr", "td", "center", "email"}
# Tags that are never closed.
VOID_TAGS = {"*", "hr"}
BRACKET = re.compile(r"\[(/?)([a-z*][a-z0-9*]*)(?:=[^\]\n]*)?\]", re.IGNORECASE)


@dataclass
class Finding:
    """Posts and occurrences for one rule, with a few examples."""
    posts: int = 0
    matches: int = 0
    examples: list[tuple[int, str]] = field(default_factory=list)

    def add(self, post_id: int, count: int, example: str, limit: int) -> None:
        self.posts += 1
        self.matches += count
        if len(self.examples) < limit:
            self.examples.append((post_id, example))


@dataclass
class ScanReport:
    posts: int = 0
    rules: dict[str, Finding] = field(default_factory=lambda: {r.name: Finding() for r in RULES})
    unbalanced: dict[str, Finding] = field(default_factory=dict)
    unknown_tags: Counter = field(default_factory=Counter)  # tag -> posts using it
    unknown_examples: dict[str, int] = field(default_factory=dict)  # tag -> a post id


def snippet(text: str, start: int, end: int, context: int = 30) -> str:
    left = max(start - context, 0)
    piece = text[left:min(end + context, len(text))].replace("\n", "⏎")
    return ("…" if left else "") + piece + ("…" if end + context < len(text) else "")


def tag_balance(text: str) -> dict[str, int]:
    """Known tag -> opens minus closes, for the tags that don't add up."""
    balance: Counter = Counter()
    for match in BRACKET.finditer(text):
        name = match.group(2).lower()
        if name in KNOWN_TAGS and name not in VOID_TAGS:
            balance[name] += -1 if match.group(1) else 1
    return {name: n for name, n in balance.items() if n}


def unknown_tags(text: str) -> set[str]:
    return {m.group(2).lower() for m in BRACKET.finditer(text)
            if m.group(2).lower() not in KNOWN_TAGS}


def scan_text(report: ScanReport, post_id: int, text: str, examples: int = 3) -> None:
    report.posts += 1
    for r in RULES:
        matches = list(r.pattern.finditer(text))
        if matches:
            first = matches[0]
            report.rules[r.name].add(post_id, len(matches),
                                     snippet(text, first.start(), first.end()), examples)
    for name, diff in tag_balance(text).items():
        kind = f"[{name}] {'unclosed' if diff > 0 else 'extra close'}"
        report.unbalanced.setdefault(kind, Finding()).add(post_id, abs(diff), "", examples)
    for name in unknown_tags(text):
        report.unknown_tags[name] += 1
        report.unknown_examples.setdefault(name, post_id)


SOURCE_COLUMNS = ("source", "source_fixed")


def iter_sources(db: Database, batch: int = 5000,
                 column: str = "source") -> Iterator[tuple[int, str]]:
    """(id, text) of posts with a non-NULL `column`, in id order."""
    assert column in SOURCE_COLUMNS
    last = -1
    while True:
        rows = db.query(f"SELECT id, {column} FROM posts WHERE id > ? AND {column} IS NOT NULL "
                        "ORDER BY id LIMIT ?", (last, batch))
        if not rows:
            return
        yield from rows
        last = rows[-1][0]


def iter_signature_sources(db: Database) -> Iterator[tuple[int, str]]:
    """(user id, BBCode) of every signature, rebuilt from its HTML."""
    for user_id, html in db.query("SELECT id, signature FROM users "
                                  "WHERE signature IS NOT NULL ORDER BY id"):
        if html.strip():
            yield user_id, html_to_bbcode(html)


def scan(db: Database, examples: int = 3, column: str = "source") -> ScanReport:
    report = ScanReport()
    for post_id, source in iter_sources(db, column=column):
        scan_text(report, post_id, source, examples)
    return report


def matching_posts(db: Database, rule_name: str, column: str = "source") -> Iterator[int]:
    pattern = RULES_BY_NAME[rule_name].pattern
    for post_id, source in iter_sources(db, column=column):
        if pattern.search(source):
            yield post_id


def print_report(report: ScanReport, out: TextIO, unknown: int = 30) -> None:
    def pct(n: int) -> str:
        return f"{100 * n / report.posts:.1f}%" if report.posts else "-"

    print(f"Scanned {report.posts:,} posts.\n", file=out)
    print("Rules (posts, occurrences):", file=out)
    for r in RULES:
        finding = report.rules[r.name]
        print(f"  {r.name:<18} {finding.posts:>8,} posts ({pct(finding.posts):>5}) "
              f"{finding.matches:>9,} hits  {r.description}", file=out)
        for post_id, example in finding.examples:
            print(f"  {'':<18}   post {post_id}: {example}", file=out)

    print("\nUnbalanced known tags (posts, surplus tags):", file=out)
    for kind, finding in sorted(report.unbalanced.items(), key=lambda kv: -kv[1].posts):
        ids = ", ".join(str(post_id) for post_id, _ in finding.examples)
        print(f"  {kind:<26} {finding.posts:>7,} posts {finding.matches:>8,} tags  e.g. {ids}",
              file=out)

    print(f"\nBracketed words that aren't tags (top {unknown} by posts; mostly plain text):",
          file=out)
    for name, posts in report.unknown_tags.most_common(unknown):
        print(f"  [{name}] {posts:,} posts (e.g. post {report.unknown_examples[name]})", file=out)
