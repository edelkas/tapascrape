"""Undo the link rewriting of a hashed dump (see dump.py).

The downloader rewrote every link to the board itself (href, src, action...:
http://metanet.2.forumer.com/index.php?Q) as ./index__q<sha256(Q)[:12]>.php,
Q hashed as written in the page (with its "&amp;"), and the board's root as
./index.html (../ from a/). The ids the parsers read from links (authors,
quote buttons, attachments, the navigation...) went with them.

A hash is undone by hashing candidates until one matches:

- the queries of every snapshot in the dump's CDX;
- every member, topic, forum and category id, as the board linked them
  (with no session id) and as members pasted them in posts;
- the board's link templates (the ones its pages use, from the flat dump),
  filled with the ids the page shows elsewhere: its own URL's, its posts',
  member numbers, topic entries..., then the forums the first ones found;
  each with no session id, an empty one, or the page's own (the board gave
  each response one, which its scripts show too);
- a link whose text is itself a board URL (pasted in a post): its query.

A link not undone points at the board's index: somewhere on the board, at
least, rather than at a file that isn't there.
"""

import re
from collections import Counter
from collections.abc import Iterable
from html import unescape
from urllib.parse import urlsplit

from tapascrape.boards.metanet.dump import query_hash

BOARD = "http://metanet.2.forumer.com"
# (the board's own pages never link "./x" or "../x": these are the downloader's)
REWRITTEN = re.compile(r"""(?P<attr>\b(?:href|src|action|data-src|data-url)=(?P<q>['"]))(?:(?:\.\./)+|\./)"""
                       r"""(?:index__q(?P<hash>[0-9a-f]{12})\.php|(?P<root>index\.html)|(?P<path>[^'"]*))(?P=q)""")
ANCHOR_TEXT = re.compile(r"\s*(?:https?://)?metanet\.2\.forumer\.com[^?<]*\?([^<\s]+)\s*</a>", re.IGNORECASE)
SESSION = re.compile(r"(?:\bs=|name=['\"]s['\"] value=['\"])([0-9a-f]{32})\b")
FORUM_ID = re.compile(r"(?:showforum|\bf)=(\d+)")
FORUMS = range(1, 101)     # forum and category ids
MEMBERS = range(1, 11001)  # old member ids reach 10,533
TOPICS = range(0, 26001)   # topic ids: 24,3xx
# Where pages show ids outside links: the posts' and topic entries' comments, author blocks, scripts.
IDS = re.compile(r"Begin Msg Number (\d+)|Topic Entry (\d+)|Member No\.:?\s*([\d,]+)|\((\d+)\)|"
                 r"entry(\d+)|[?&;](?:showtopic|showuser|showforum|t|f|p|MID|id)=(\d+)")

# Every member, topic, forum and category, as links with no session id (or an empty one)...
WHOLE_RANGES = (("showuser={n}", MEMBERS), ("showtopic={n}", TOPICS), ("showforum={n}", FORUMS),
                ("act=SF&f={n}", FORUMS), ("act=SC&c={n}", FORUMS), ("c={n}", FORUMS))
# ... as members pasted them in posts and signatures...
PASTED = (("showtopic={n}&hl=", TOPICS), ("showtopic={n}&st=0", TOPICS), ("showtopic={n}&view=getnewpost", TOPICS),
          ("showtopic={n}&view=getlastpost", TOPICS), ("showforum={n}&st=0", FORUMS))
# ... and with each page's session ids (the board's links: written with "&amp;")
SESSION_RANGES = (("showuser={n}", MEMBERS), ("showforum={n}", FORUMS), ("act=SF&f={n}", FORUMS),
                  ("act=SC&c={n}", FORUMS), ("c={n}", FORUMS))

# Templates with one id ({n}); "&" stands for "&amp;" too, as pages write it.
SINGLE = ("showuser={n}", "showtopic={n}", "showtopic={n}&view=getlastpost", "showtopic={n}&view=getnewpost",
          "showtopic={n}&view=old", "showtopic={n}&view=new", "showforum={n}", "c={n}", "act=SC&c={n}",
          "act=SF&f={n}", "act=Msg&CODE=04&MID={n}", "act=Mail&CODE=00&MID={n}", "act=Attach&type=post&id={n}",
          "act=Post&CODE=00&f={n}", "act=Search&f={n}", "act=Track&f={n}&type=forum", "act=Profile&MID={n}",
          "act=Search&CODE=getalluser&mid={n}", "act=Members&max_results=30&filter={n}")
# ... with a forum ({f}) and a topic ({t}), and a post ({p})
TOPIC = ("act=Track&f={f}&t={t}", "act=Forward&f={f}&t={t}", "act=Print&client=printer&f={f}&t={t}",
         "act=Print&client=choose&f={f}&t={t}", "act=Post&CODE=02&f={f}&t={t}", "act=Post&CODE=06&f={f}&t={t}",
         "act=Poll&t={t}", "act=ST&f={f}&t={t}", "act=ST&f={f}&t={t}&view=getnewpost",
         "act=ST&f={f}&t={t}&view=getlastpost")
POST = ("act=Post&CODE=06&f={f}&t={t}&p={p}", "act=Post&CODE=08&f={f}&t={t}&p={p}",
        "act=Post&CODE=04&f={f}&t={t}&p={p}", "act=Post&CODE=02&f={f}&t={t}&p={p}",
        "showtopic={t}&view=findpost&p={p}", "act=ST&f={f}&t={t}&view=findpost&p={p}")
PAGES = ("showtopic={t}&st={n}", "act=ST&f={f}&t={t}&st={n}", "showforum={f}&st={n}")
PAGE_STARTS = set(range(0, 3000, 15)) | set(range(0, 3000, 20))
BOARD_LINKS = ("act=idx", "act=Help", "act=Search", "act=Members", "act=calendar", "act=boardrules", "act=SF",
               "act=Login&CODE=00", "act=Login&CODE=03", "act=Reg&CODE=00", "act=Reg&CODE=reval",
               "act=Login&CODE=01&CookieDate=1", "act=Online", "act=Stats", "act=Stats&CODE=leaders",
               "act=Search&CODE=getnew", "act=Search&CODE=getactive", "act=Login&CODE=06", "act=Members&CODE=",
               "act=legends&CODE=emoticons", "act=Msg&CODE=01", "act=UserCP&CODE=00")


class LinkRestorer:
    def __init__(self, urls: Iterable[str]):
        """`urls`: those of the dump's snapshots (its CDX)."""
        self.known: dict[str, str] = {}  # hash -> the query as written
        for url in urls:
            if query := urlsplit(url).query:
                self._add(self.known, query)
        for prefix in ("", "s=&"):
            for template, ids in WHOLE_RANGES:
                for n in ids:
                    self._add(self.known, prefix + template.format(n=n))
        for template, ids in PASTED:
            for n in ids:
                self._add(self.known, template.format(n=n))
        self.counts: Counter = Counter()

    @staticmethod
    def _add(table: dict[str, str], query: str, plain: bool = True) -> None:
        """A query as pages may write it: with "&amp;", or (`plain`) "&"."""
        query = query.replace("&amp;", "&")
        for written in (query, query.replace("&", "&amp;")) if plain else (query.replace("&", "&amp;"),):
            table.setdefault(query_hash(written), written)

    def restore(self, page: str, own: dict[str, str]) -> str:
        """The page with the links the downloader rewrote pointing at the board again (`own`: the
        page's own query)."""
        hashes = {m.group("hash") for m in REWRITTEN.finditer(page) if m.group("hash")}
        if not hashes:
            return page
        found = {h: self.known[h] for h in hashes if h in self.known}
        sessions = set(SESSION.findall(page)) | ({own["s"]} if own.get("s") else set())
        prefixes = ["", "s=&"] + [f"s={s}&" for s in sorted(sessions)]
        numbers = {int(n.replace(",", "")) for match in IDS.findall(page) for n in match if n}
        numbers |= {int(v) for v in own.values() if v.isdigit()}
        topics = {int(own[k]) for k in ("showtopic", "t") if own.get(k, "").isdigit()}
        posts = {int(p) for p in re.findall(r"Begin Msg Number (\d+)", page)}

        def fill(templates, prefixes=prefixes, plain=True, **ids) -> None:
            for template in templates:
                if not hashes - found.keys():
                    return
                table: dict[str, str] = {}
                names = [n for n in ("n", "f", "t", "p") if "{" + n + "}" in template]
                for combo in _combos([ids[n] for n in names]):
                    query = template.format(**dict(zip(names, combo)))
                    for prefix in prefixes:
                        self._add(table, prefix + query, plain)
                found.update({h: table[h] for h in hashes - found.keys() if h in table})

        fill(BOARD_LINKS)
        fill(SINGLE, n=numbers)
        for template, ids in SESSION_RANGES:
            fill((template,), prefixes[2:], plain=False, n=ids)
        forums = {int(m.group(1)) for query in found.values() if (m := FORUM_ID.search(query))}
        fill(TOPIC, f=forums or set(FORUMS), t=topics)
        forums = {int(m.group(1)) for query in found.values() if (m := FORUM_ID.search(query))}
        fill(POST, f=forums, t=topics, p=posts)
        fill(PAGES, f=forums, t=topics, n=PAGE_STARTS)

        def replace(match: re.Match) -> str:
            if match.group("root"):
                return f"{match.group('attr')}{BOARD}/{match.group('q')}"
            if match.group("hash") is None:  # another of the board's files (style_images/...)
                return f"{match.group('attr')}{BOARD}/{match.group('path')}{match.group('q')}"
            query = found.get(match.group("hash"))
            if query is None and (text := ANCHOR_TEXT.match(page, page.find(">", match.end()) + 1)):
                shown: dict[str, str] = {}  # pasted in a post: its text shows the URL
                self._add(shown, unescape(text.group(1)))
                query = shown.get(match.group("hash"))
            if query is None:
                self.counts["links not restored"] += 1
                return f"{match.group('attr')}{BOARD}/index.php{match.group('q')}"
            self.counts["links restored"] += 1
            return f"{match.group('attr')}{BOARD}/index.php?{query}{match.group('q')}"

        return REWRITTEN.sub(replace, page)


def _combos(sets: list) -> Iterable[tuple]:
    if not sets:
        yield ()
        return
    for first in sets[0]:
        for rest in _combos(sets[1:]):
            yield (first, *rest)
