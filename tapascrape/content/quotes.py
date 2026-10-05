"""Find the posts that quotes quote: posts' [quote] tags -> the quotes table.

Every [quote] of a post's text (source_fixed, else source, else the HTML
rebuilt as BBCode) becomes a row, nested ones included: `position` numbers
the opening tags in text order (outside [code] blocks), `level` is 1 for a
quote written in the post itself, 2 for a quote inside it, and so on, and
`parent_position` is the enclosing quote's position.

A quote can say who and when ([quote="name" date="..."], [quote=name @ date],
[quote=name,date], or explicit ids like post=/uid=/timestamp=), and its body
is (usually) the quoted post's text, so three kinds of evidence point at it:

  author  the name matches the candidate post's author (exactly, or nearly)
  date    the date is the candidate's time as shown to the quoter: in their
          timezone, so off by a real timezone's offset (whole or half hours, or
          the :45 zones, -12h to +14h), and truncated to the minute. Each
          quoter's usual offset breaks ties.
  text    the share of the body's word trigrams found in the candidate's own
          text (its quotes left out, as the quote's own nested quotes are)

The quoted post must be older than the post quoting it (or, for a nested
quote, than the quote that contains it). The match column says what agreed:
"post-id", "author+date", "date+text", "author+text", "near-author+date",
"text" (most of it, in the same topic: elsewhere, the same text is as likely
quoted from where both took it), "date" (the only post of the topic at that
time, no author to check)
or "author" (the author's latest earlier post in the topic, for bodies too
short to compare). Author and date alone don't do when the body is long enough
to compare and isn't in the post, unless the offset is the quoter's usual one
and the post is in the same topic (posts get edited). Unmatched quotes have no
quoted_post_id; "ambiguous" ones had several equally good candidates.

Boards may know names the users table doesn't (renamed members, guests):
pass them as `aliases` (user id -> names) and `authors` (post id -> name).
"""

import bisect
import difflib
import html
import logging
import re
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from tapascrape.crawl.progress import Progress
from tapascrape.db.base import Database
from tapascrape.parse.bbcode import html_to_bbcode

log = logging.getLogger(__name__)

# -- parsing -------------------------------------------------------------------------------

TOKEN = re.compile(r"(\[code\b[^\]]*\].*?\[/code\])|\[quote(?=[=\s\]])([^\]\n]*)\]|(\[/quote\])",
                   re.IGNORECASE | re.DOTALL)
ATTRIBUTE = re.compile(r"""(\w+)\s*=\s*("(?:[^"\\]|\\.)*"|'[^']*'|[^\s"']+)""")
POST_ATTRIBUTES = {"post", "post_id", "postid", "pid", "p"}
USER_ATTRIBUTES = {"uid", "user_id", "userid", "member", "member_id"}
TIME_ATTRIBUTES = {"timestamp", "time"}
XENFORO_IDS = re.compile(r",\s*(post|member):\s*(\d+)", re.IGNORECASE)


@dataclass
class QuoteDate:
    year: int
    month: int | None  # None when the attribution lost it ("name @  25, 2007 03:11 pm")
    day: int
    hour: int | None   # None for a day only
    minute: int | None
    utc: bool = False  # from a unix timestamp: no timezone offset

    @property
    def exact(self) -> datetime | None:
        if self.month is None or self.hour is None:
            return None
        try:
            return datetime(self.year, self.month, self.day, self.hour, self.minute)
        except ValueError:
            return None

    def fits(self, local: datetime) -> bool:
        return (local.year == self.year and local.day == self.day
                and (self.month is None or local.month == self.month)
                and (self.hour is None or (local.hour, local.minute) == (self.hour, self.minute)))


@dataclass
class Quote:
    position: int
    level: int
    parent: int | None           # position of the enclosing quote
    author: str | None = None
    date: str | None = None      # as written
    when: QuoteDate | None = None
    post_ref: int | None = None  # explicit ids, when the tag has them
    user_ref: int | None = None
    body: str = ""               # inner text, nested quotes left out
    start: int = 0               # the tag pair's span in the text
    end: int = 0
    closed: bool = False
    _inner: tuple[int, int] = (0, 0)
    _children: list["Quote"] = field(default_factory=list)


def parse_quotes(text: str) -> list[Quote]:
    """The text's quotes, in opening-tag order. Unclosed quotes run to the end; stray
    closing tags are ignored."""
    quotes: list[Quote] = []
    stack: list[Quote] = []
    for m in TOKEN.finditer(text):
        if m.group(1):
            continue
        if m.group(3):
            if stack:
                quote = stack.pop()
                quote.closed = True
                quote.end, quote._inner = m.end(), (quote._inner[0], m.start())
            continue
        parent = stack[-1] if stack else None
        quote = Quote(len(quotes), len(stack) + 1, parent.position if parent else None,
                      start=m.start(), end=len(text), _inner=(m.end(), len(text)))
        read_attribution(quote, m.group(2))
        if parent:
            parent._children.append(quote)
        quotes.append(quote)
        stack.append(quote)
    for quote in quotes:
        inner_start, inner_end = quote._inner
        quote.end = max(quote.end, inner_end)
        pieces, at = [], inner_start
        for child in quote._children:
            pieces.append(text[at:child.start])
            at = child.end
        pieces.append(text[at:inner_end])
        quote.body = " ".join(pieces)
    return quotes


def own_text(text: str, quotes: list[Quote]) -> str:
    """The text without its quotes (an unclosed one may well hold the post's own words)."""
    pieces, at = [], 0
    for quote in quotes:
        if quote.level == 1 and quote.closed:
            pieces.append(text[at:quote.start])
            at = quote.end
    pieces.append(text[at:])
    return " ".join(pieces)


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        mark, value = value[0], value[1:-1]
        if mark == '"':
            value = re.sub(r"\\(.)", r"\1", value)
    return value


def read_attribution(quote: Quote, attrs: str) -> None:
    """Fill author/date/ids from what follows "[quote" in the opening tag."""
    attrs = attrs.strip()
    default = None
    if attrs.startswith("="):
        rest = attrs[1:].strip()
        m = re.match(r'"(?:[^"\\]|\\.)*"|\'[^\']*\'', rest)
        if m and (not rest[m.end():].strip() or ATTRIBUTE.match(rest[m.end():].strip())):
            default, attrs = _unquote(m.group(0)), rest[m.end():]
        else:
            default, attrs = rest, ""
    named = {key.lower(): _unquote(value) for key, value in ATTRIBUTE.findall(attrs)}
    for key, value in named.items():
        if key in POST_ATTRIBUTES and value.isdigit() and int(value) > 0:
            quote.post_ref = int(value)
        elif key in USER_ATTRIBUTES and value.isdigit() and int(value) > 0:  # uid=0: a guest
            quote.user_ref = int(value)
        elif key in TIME_ATTRIBUTES and value.isdigit():
            if int(value) > 0:
                moment = datetime.fromtimestamp(int(value), timezone.utc)
                quote.date = value
                quote.when = QuoteDate(moment.year, moment.month, moment.day, moment.hour,
                                       moment.minute, utc=True)
    if default is None:
        default = named.get("name") or named.get("author")
    if default:
        for kind, value in XENFORO_IDS.findall(default):
            if kind.lower() == "post":
                quote.post_ref = quote.post_ref or int(value)
            else:
                quote.user_ref = quote.user_ref or int(value)
        default = XENFORO_IDS.sub("", default)
    date_text = named.get("date") or (named.get("time") if quote.when is None else None)
    if date_text:
        quote.date, quote.when = date_text, parse_date(date_text)
        quote.author = clean_author(default or "") or None
    elif quote.when is not None:
        quote.author = clean_author(default or "") or None
    elif default:
        quote.author, quote.date, quote.when = split_attribution(default)


MONTHS = {name: n for n, names in enumerate(
    (("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"), ("may",),
     ("jun", "june"), ("jul", "july"), ("aug", "august"), ("sep", "sept", "september"),
     ("oct", "october"), ("nov", "november"), ("dec", "december")), 1) for name in names}
# On text with "@", "," and no-break spaces blanked out, ending the attribution:
# "October 30   2006 06:56 am", "Jan 26 2005  10:02 PM", "  25  2007 03:11 pm", "July 11th  1941"
DATE = re.compile(r"(?:\b([a-z]{3,9})\.?\s+)?\b(\d{1,2})(?:st|nd|rd|th)?\s+(\d{4})"
                  r"(?:\s+(\d{1,2}):(\d{2})\s*([ap])\.?m\.?)?[\s)]*$")
SEPARATORS = str.maketrans({"@": " ", ",": " ", "\xa0": " "})


def _date_match(text: str) -> tuple[re.Match, QuoteDate] | None:
    m = DATE.search(text.translate(SEPARATORS).lower())
    if m is None:
        return None
    month_name, day, year, hour, minute, ampm = m.groups()
    month = MONTHS.get(month_name) if month_name else None
    if not (1990 <= int(year) <= 2100 and 1 <= int(day) <= 31):
        return None
    when = QuoteDate(int(year), month, int(day), None, None)
    if hour is not None:
        h = int(hour) % 12 + (12 if ampm == "p" else 0)
        if h > 23 or int(minute) > 59:
            return None
        when.hour, when.minute = h, int(minute)
    return m, when


def parse_date(text: str) -> QuoteDate | None:
    found = _date_match(text)
    return found[1] if found else None


def split_attribution(value: str) -> tuple[str | None, str | None, QuoteDate | None]:
    """'name @ date', 'name,date', '(name @ date)', 'name posted on date'... -> parts."""
    value = value.strip()
    if value.startswith("(") and value.endswith(")"):
        value = value[1:-1]
    found = _date_match(value)
    if found is None:
        return clean_author(value) or None, None, None
    m, when = found
    start = m.start(1) if m.group(1) and when.month else m.start(2)
    author = clean_author(value[:start])
    return author or None, value[start:].strip(" )"), when


def clean_author(text: str) -> str:
    text = re.sub(r"(?:\s|@|,|\()+$", "", text.replace("\xa0", " "))
    text = re.sub(r"\s+(?:posted\s+on|posted|wrote|said)$", "", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip(" (")


# -- text similarity ---------------------------------------------------------------------

TAG = re.compile(r"\[/?[a-z*][^\]\n]*\]|<[^>\n]*>", re.IGNORECASE)
WORD = re.compile(r"\w+")
MAX_SHINGLES = 60       # per quote, spread over the body
COMMON = 2000           # trigrams in more posts than this say nothing


def words(text: str) -> list[str]:
    return WORD.findall(html.unescape(TAG.sub(" ", text)).lower())


def shingles(text: str) -> set[int]:
    w = words(text)
    return {hash((w[i], w[i + 1], w[i + 2])) for i in range(len(w) - 2)}


def sample(items: set[int], limit: int = MAX_SHINGLES) -> set[int]:
    if len(items) <= limit:
        return items
    ordered = sorted(items)
    return {ordered[i * len(ordered) // limit] for i in range(limit)}


# -- names -------------------------------------------------------------------------------

def name_key(name: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(name)).strip().casefold()


def name_score(author: str | None, names: set[str]) -> int:
    """2: the author is one of the names; 1: nearly (typo, prefix); 0: no."""
    if not author or not names:
        return 0
    key = name_key(author)
    if key in names:
        return 2
    if len(key) >= 3:
        for name in names:
            if len(name) < 3 or abs(len(name) - len(key)) > max(len(name), len(key)) * 0.3 + 2:
                continue
            if name.startswith(key) or key.startswith(name):
                return 1
            matcher = difflib.SequenceMatcher(None, key, name)
            if matcher.quick_ratio() >= 0.85 and matcher.ratio() >= 0.85:
                return 1
    return 0


# -- matching ------------------------------------------------------------------------------

# Minutes the quoter's timezone could be off UTC: whole and half hours, and the :45 zones.
OFFSETS = sorted({*range(-12 * 60, 14 * 60 + 1, 30), 345, 525, 765})
OFFSET_SET = set(OFFSETS)
MIN_TEXT = 3                                # trigrams a body needs to be compared at all
EARLIER_SLACK = timedelta(minutes=2)
TOPIC_SCAN = 500                            # same-topic posts looked at, latest first


def as_datetime(value) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def tz_offset(when: QuoteDate, posted: datetime) -> int | None:
    """Minutes to add to `posted` (UTC) to show it as the quote does, closest to 0."""
    minute = posted.replace(second=0, microsecond=0)
    if when.utc:
        exact = when.exact
        return 0 if exact and abs((exact - minute).total_seconds()) <= 60 else None
    exact = when.exact
    if exact is not None:
        diff = round((exact - minute).total_seconds() / 60)
        offset = round(diff / 15) * 15
        return offset if abs(diff - offset) <= 1 and offset in OFFSET_SET else None
    for offset in sorted(OFFSETS, key=abs):
        if when.fits(minute + timedelta(minutes=offset)):
            return offset
    return None


@dataclass
class Post:
    id: int
    topic_id: int
    user_id: int | None
    posted: datetime | None


@dataclass
class Found:
    post: Post
    how: str
    rank: tuple
    offset: int | None
    similarity: float | None


class Matcher:
    def __init__(self, posts: dict[int, Post], names: dict[int, set[str]], authors: dict[int, str]):
        self.posts = posts
        self.names = names            # user id -> name keys
        self.authors = authors        # post id -> name key (posts without a user)
        timed = sorted((p.posted, p.id) for p in posts.values() if p.posted is not None)
        self.times = [t for t, _ in timed]
        self.timed_ids = [i for _, i in timed]
        by_topic: dict[int, list[tuple]] = defaultdict(list)
        for post in posts.values():
            if post.posted is not None:
                by_topic[post.topic_id].append((post.posted, post.id))
        self.by_topic = {topic: sorted(rows) for topic, rows in by_topic.items()}
        self.users_by_name: dict[str, set[int]] = defaultdict(set)
        for user_id, keys in names.items():
            for key in keys:
                self.users_by_name[key].add(user_id)
        self.habits: dict[int, Counter] = defaultdict(Counter)  # user -> tz offsets seen
        self._scores: dict[tuple[str, int], int] = {}

    def author_score(self, quote: Quote, post: Post) -> int:
        if quote.user_ref is not None and post.user_id == quote.user_ref:
            return 2
        if not quote.author:
            return 0
        key = name_key(quote.author)
        score = 0
        if post.user_id:
            cached = self._scores.get((key, post.user_id))
            if cached is None:
                cached = self._scores[key, post.user_id] = name_score(key, self.names.get(post.user_id, set()))
            score = cached
        if score < 2 and post.id in self.authors:
            score = max(score, name_score(key, {self.authors[post.id]}))
        return score

    def candidates(self, quote: Quote, topic_id: int, before: datetime | None,
                   text_hits: Counter) -> dict[int, Post]:
        """Posts worth weighing: those with the most of the text, and the author's around the
        date or in the topic (any post of the topic at that time, for the "date" match)."""
        found = {post_id: self.posts[post_id] for post_id, _ in text_hits.most_common(20)
                 if post_id in self.posts}
        exact = quote.when.exact if quote.when else None
        if exact is not None:
            low = bisect.bisect_left(self.times, exact - timedelta(minutes=OFFSETS[-1] + 2))
            high = bisect.bisect_right(self.times, exact - timedelta(minutes=OFFSETS[0] - 2))
            for post_id in self.timed_ids[low:high]:
                post = self.posts[post_id]
                if post.topic_id == topic_id or self.author_score(quote, post):
                    found[post_id] = post
        if quote.author or quote.user_ref:
            rows = self.by_topic.get(topic_id, [])
            end = bisect.bisect_right(rows, (before + EARLIER_SLACK, float("inf"))) if before else len(rows)
            for _, post_id in rows[max(0, end - TOPIC_SCAN):end]:
                post = self.posts[post_id]
                if self.author_score(quote, post):
                    found[post_id] = post
        return found

    def learn(self, quote: Quote, viewer: int | None, candidates: list[tuple]) -> None:
        """Remember the viewer's timezone offset when one candidate alone has author and date."""
        if viewer is None:
            return
        sure = [offset for _, author, offset, _ in candidates if author == 2 and offset is not None]
        if len(sure) == 1:
            self.habits[viewer][sure[0]] += 1

    def usual(self, viewer: int | None) -> set[int]:
        seen = self.habits.get(viewer)
        if not seen:
            return set()
        total = sum(seen.values())
        return {offset for offset, n in seen.items() if n >= 2 and n >= total / 5}

    def evidence(self, quote: Quote, container: Post, before: datetime | None, pool: dict[int, Post],
                 text_hits: Counter, comparable: int) -> list[tuple]:
        """(post, author score, tz offset or None, similarity or None) of each earlier candidate."""
        out = []
        for post in pool.values():
            if post.id == container.id or post.posted is None:
                continue
            if before is not None and post.posted > before + EARLIER_SLACK:
                continue
            author = self.author_score(quote, post)
            offset = tz_offset(quote.when, post.posted) if quote.when else None
            similarity = text_hits[post.id] / comparable if comparable >= MIN_TEXT else None
            out.append((post, author, offset, similarity))
        return out

    def decide(self, quote: Quote, container: Post, viewer: int | None,
               candidates: list[tuple]) -> Found | str | None:
        """The quoted post, "ambiguous", or None."""
        if quote.post_ref is not None and quote.post_ref in self.posts:
            return Found(self.posts[quote.post_ref], "post-id", (), None, None)
        usual = self.usual(viewer)
        named = quote.author is not None and name_key(quote.author) in self.users_by_name
        best: list[tuple] = []
        for post, author, offset, similarity in candidates:
            text = similarity or 0
            dated = offset is not None
            same_topic = post.topic_id == container.topic_id
            habit = dated and (offset in usual if usual else offset == 0)
            if similarity is not None and similarity < 0.2 and not (habit and same_topic):
                dated = False  # the text says it's another post, and only chance says it's this one
            if dated and author == 2:
                how, tier = "author+date", 0
            elif dated and text >= 0.5:
                how, tier = "date+text", 1
            elif author and text >= 0.5:
                how, tier = "author+text", 2
            elif dated and author == 1 and (similarity is None or similarity >= 0.3):
                how, tier = "near-author+date", 3
            elif text >= 0.8 and same_topic:  # elsewhere, the same text is as likely quoted
                how, tier = "text", 4             # from where they both took it
            elif (dated and quote.when.exact and same_topic and not named
                  and similarity is None):
                how, tier = "date", 5
            elif (author == 2 and similarity is None and same_topic and not quote.when):
                how, tier = "author", 6
            else:
                continue
            rank = (-tier, habit, round(text, 2), author, same_topic)
            best.append((rank, post.posted, post, how, offset, similarity))
        if not best:
            return None
        best.sort(key=lambda b: (b[0], b[1]), reverse=True)
        top = best[0]
        if len(best) > 1 and best[1][0] == top[0] and top[3] not in ("text", "author"):
            return "ambiguous"  # equally good; the latest only decides repeated text
        if top[3] == "date" and len(best) > 1 and best[1][3] == "date":
            return "ambiguous"
        return Found(top[2], top[3], top[0], top[4], top[5])


# -- the command ---------------------------------------------------------------------------

def post_text(source_fixed: str | None, source: str | None, content: str | None) -> str | None:
    if source_fixed is not None:
        return source_fixed
    if source is not None:
        return source
    return html_to_bbcode(content) if content else None


def iter_texts(db: Database, batch: int = 5000, quoting: bool = False) -> Iterator[tuple[int, str]]:
    """(post id, text) of every post (only those with quotes when `quoting`)."""
    where = ("AND (source_fixed LIKE '%[quote%' OR source LIKE '%[quote%' OR "
             "(source IS NULL AND content LIKE '%quote%'))") if quoting else ""
    last = -1
    while True:
        rows = db.query("SELECT id, source_fixed, source, CASE WHEN source IS NULL THEN content END "
                        f"FROM posts WHERE id > ? {where} ORDER BY id LIMIT ?", (last, batch))
        if not rows:
            return
        for post_id, *texts in rows:
            text = post_text(*texts)
            if text:
                yield post_id, text
        last = rows[-1][0]


def load_names(db: Database, aliases: dict[int, set[str]] | None) -> dict[int, set[str]]:
    names: dict[int, set[str]] = defaultdict(set)
    for user_id, name in db.query("SELECT id, name FROM users"):
        if name:
            names[user_id].add(name_key(name))
    for user_id, more in (aliases or {}).items():
        names[user_id].update(name_key(n) for n in more if n)
    return names


def link_quotes(db: Database, aliases: dict[int, set[str]] | None = None,
                authors: dict[int, str] | None = None) -> Counter:
    """Rebuild the quotes table from every post's text."""
    db.create_schema()
    posts = {row[0]: Post(row[0], row[1], row[2], as_datetime(row[3]))
             for row in db.query("SELECT id, topic_id, user_id, timestamp FROM posts")}
    matcher = Matcher(posts, load_names(db, aliases),
                      {post_id: name_key(name) for post_id, name in (authors or {}).items() if name})

    # 1. Every quote, and the trigrams of its body to look for.
    quoted: dict[int, list[Quote]] = {}
    wanted: dict[tuple[int, int], set[int]] = {}
    for post_id, text in iter_texts(db, quoting=True):
        quotes = parse_quotes(text)
        if quotes:
            quoted[post_id] = quotes
            for quote in quotes:
                wanted[post_id, quote.position] = sample(shingles(quote.body))
    total = sum(len(q) for q in quoted.values())
    log.info("quotes: %d quotes in %d posts", total, len(quoted))

    # 2. Which posts' own text has them.
    needed = set().union(*wanted.values()) if wanted else set()
    postings: dict[int, list[int] | None] = {}
    progress = Progress("quotes: indexing post text", len(posts))
    for n, (post_id, text) in enumerate(iter_texts(db), 1):
        if n % 5000 == 0:
            progress.advance(5000)
        quotes = quoted.get(post_id)
        for sh in shingles(own_text(text, quotes) if quotes else text) & needed:
            hits = postings.setdefault(sh, [])
            if hits is not None:
                hits.append(post_id)
                if len(hits) > COMMON:
                    postings[sh] = None

    # 3. Match, level by level (a nested quote is older than the one containing it).
    found: dict[tuple[int, int], Found | str | None] = {}
    report: Counter = Counter()
    progress = Progress("quotes: matching", total)
    for level in range(1, max((q.level for qs in quoted.values() for q in qs), default=0) + 1):
        pending = []
        for post_id, quotes in quoted.items():
            container = posts.get(post_id)
            if container is None:
                continue
            for quote in quotes:
                if quote.level != level:
                    continue
                before, viewer = container.posted, container.user_id
                if quote.parent is not None:
                    parent = found.get((post_id, quote.parent))
                    if isinstance(parent, Found):
                        before, viewer = parent.post.posted, parent.post.user_id
                hits: Counter = Counter()
                usable = [sh for sh in wanted[post_id, quote.position] if postings.get(sh)]
                for sh in usable:
                    hits.update(postings[sh])
                pool = matcher.candidates(quote, container.topic_id, before, hits)
                evidence = matcher.evidence(quote, container, before, pool, hits, len(usable))
                matcher.learn(quote, viewer, evidence)
                pending.append((post_id, quote, container, viewer, evidence))
                progress.advance(detail=f"level {level}")
        for post_id, quote, container, viewer, evidence in pending:
            found[post_id, quote.position] = matcher.decide(quote, container, viewer, evidence)

    # 4. Store.
    rows = []
    for post_id, quotes in quoted.items():
        for quote in quotes:
            result = found.get((post_id, quote.position))
            row = {"post_id": post_id, "position": quote.position, "level": quote.level,
                   "parent_position": quote.parent, "author": quote.author, "date": quote.date,
                   "quoted_post_id": None, "quoted_user_id": None, "match": None,
                   "similarity": None, "tz_offset": None}
            if isinstance(result, Found):
                row.update(quoted_post_id=result.post.id, quoted_user_id=result.post.user_id or None,
                           match=result.how, tz_offset=result.offset,
                           similarity=None if result.similarity is None
                           else round(100 * min(result.similarity, 1.0)))
                report[f"matched by {result.how}"] += 1
            else:
                row["match"] = result
                users = matcher.users_by_name.get(name_key(quote.author)) if quote.author else None
                if quote.user_ref is not None:
                    row["quoted_user_id"] = quote.user_ref
                elif users and len(users) == 1:
                    row["quoted_user_id"] = next(iter(users))
                report["ambiguous" if result else "unmatched"] += 1
            rows.append(row)
    with db.transaction():
        db.execute("DELETE FROM quotes")
        for i in range(0, len(rows), 5000):
            db.upsert_many("quotes", rows[i:i + 5000])
    report["quotes"] = total
    report["posts with quotes"] = len(quoted)
    return report
