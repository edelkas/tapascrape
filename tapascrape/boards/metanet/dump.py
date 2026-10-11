"""The Wayback Machine dumps of the Forumer board, as saved on disk.

Three layouts, from the downloaders that made them:

- flat (the 2019 dump): root files are the board's pages, named after their
  URL with "?" turned into "_" (index.php_s=&showtopic=19141,
  index.php_act=ST&f=5&t=5031, sometimes with "&amp;" or a session id).
- hashed (StrawberryMaster's wayback-machine-downloader, --local): a page's
  query is hashed into its name (index.php?Q -> index__q<sha256(Q)[:12]>.php).
  Its .cdx.json lists every snapshot ([timestamp, url]); each name is the
  latest snapshot of its URLs, so the name gives back the URL and the capture
  date (cdx_file_id repeats how the downloader names them). --local also
  rewrote the board's own links in the pages the same way, and dropped their
  non-ASCII bytes: relink.py restores the links.
- snapshots (the same downloader, --all-timestamps, without --local): every
  snapshot, in a folder per capture time (<timestamp>/<name>), named as in a
  hashed dump; the pages are as the board served them. Its .downloaded.txt
  lists them as <timestamp>/<name>. Snapshots the CDX lists but it lacks are
  redirects (view=getlastpost...), which have no page.

All have a/, Forumer's "lite archive" (one page per 15 posts of a topic, plus
a member index). Many pages are useless: login-only pages answer with an error
("Board Message"), session redirects land on the board index, and some are a
parked domain's or a MySQL error page.

iter_dumps reads several dumps as one, each capture once: a snapshots dump's
copy first (lossless and dated), else a flat dump's, else a hashed dump's. A
flat dump's file of the same size as a snapshot of its URL is that snapshot.
Pages come oldest capture first (undated first), so whatever merges them can
let the latest win (see newer).
"""

import hashlib
import json
import re
from collections import Counter, deque
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

# act=... value -> kind of page we care about
ACT_KINDS = {"st": "topic", "sf": "forum", "attach": "attach", "members": "members",
             "legends": "emoticons", "print": "print"}
CDX = ".cdx.json"
DOWNLOADED = ".downloaded.txt"
# The board reopened in 2011-2012 ("We're Back!") with a joke word filter (little -> cilantro,
# job -> burrito, girl -> steak...), first seen in 2012-03-24's captures (February's have none).
FILTERED_SINCE = datetime(2012, 3, 1)


@dataclass(frozen=True)
class DumpFile:
    path: Path
    kind: str  # topic, profile, forum, attach, members, emoticons, archive-topic, archive-members, other
    query: dict[str, str]
    source: str = ""                    # what source_file and crawl_state call it; default the file name
    captured_at: datetime | None = None
    restorer: object = field(default=None, compare=False, repr=False)  # relink.LinkRestorer, hashed dumps

    @property
    def name(self) -> str:
        return self.source or self.path.name

    @property
    def lossy(self) -> bool:
        """Its non-ASCII characters were dropped (a hashed dump's page): what it shows only
        replaces what another copy showed when more than those differ (see newer)."""
        return self.restorer is not None

    @property
    def filtered(self) -> bool:
        """Captured while the word filter may have been on: its text doesn't replace what
        earlier copies showed where it only differs as the filter changed it (see newer)."""
        return self.captured_at is not None and self.captured_at >= FILTERED_SINCE

    def read(self) -> str:
        return self.text(self.path.read_bytes())

    def text(self, data: bytes) -> str:
        """The page, from the file's bytes (as read_ahead reads them)."""
        if self.restorer is None:
            return decode(data)
        return self.restorer.restore(data.decode("utf-8", "replace"), self.query)


def decode(data: bytes) -> str:
    """Pages are Windows-1252 (served as ISO-8859-1); latin-1 for the odd byte cp1252 lacks."""
    try:
        return data.decode("cp1252")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def parse_query(query: str) -> dict[str, str]:
    """A URL's query parameters (keys lowercased)."""
    query = query.replace("&amp;", "&")
    return {k.lower(): v for k, v in parse_qsl(query, keep_blank_values=True)}


def query_of(name: str) -> dict[str, str]:
    """The query parameters of a flat dump's root file's URL (keys lowercased)."""
    return parse_query(name.split("_", 1)[1] if name.startswith("index.php_") else "")


def kind_of(query: dict[str, str]) -> str:
    if "showtopic" in query:
        return "topic"
    if "showuser" in query:
        return "profile"
    if "showforum" in query:
        return "forum"
    return ACT_KINDS.get(query.get("act", "").lower(), "other")


ARCHIVE_TOPIC = re.compile(r"_post(\d+)(?:-(\d+))?\.html$")


def archive_position(name: str) -> tuple[int, int] | None:
    """(topic id, offset of the page's first post) of a lite-archive page name."""
    match = ARCHIVE_TOPIC.search(name)
    return (int(match.group(1)), int(match.group(2) or 0)) if match else None


def newer(old, new, copy: DumpFile | None = None):
    """`new`, a later copy's value, unless it's missing, or text from a filtered copy that only
    differs as the filter changed it, or from a lossy copy, only lacking the characters it
    dropped and the links it couldn't restore. (Numbers and dates from either are the latest.)"""
    if new is None or new == "":
        return old
    if copy is not None and isinstance(old, str) and old and isinstance(new, str):
        if (copy.filtered and only_filtered(old, new)) or (copy.lossy and _lossy_form(old) == _lossy_form(new)):
            return old
    return new


WORDS = re.compile(r"\w+|[^\w\s]+")


def only_filtered(old: str, new: str) -> bool:
    """Whether `new` only differs from `old` as the 2012 word filter changed text: a word or
    two replaced by lowercase ones ("Free stuff" -> "delicious stuff", "Horror Movies" ->
    "Horror food network"), or case."""
    a, b = WORDS.findall(old), WORDS.findall(new)
    matcher = SequenceMatcher(None, [w.lower() for w in a], [w.lower() for w in b], autojunk=False)
    return all(op == "equal" or (op == "replace" and i2 - i1 <= 2 and j2 - j1 <= 2
                                 and all(w.isalpha() and w.islower() for w in b[j1:j2]))
               for op, i1, i2, j1, j2 in matcher.get_opcodes())


NON_ASCII = re.compile(r"[^\x00-\x7f]")
LINK_ATTRIBUTE = re.compile(r"""\b(href|src|action)=(['"])[^'"]*\2""")


def _lossy_form(value: str) -> str:
    return LINK_ATTRIBUTE.sub(r"\1=", NON_ASCII.sub("", value))


def is_error(page: str) -> bool:
    """A login-required/error page, a MySQL failure or a parked-domain answer."""
    head = page[:6000]
    return ("<title>Board Message</title>" in head or "Can't connect to local MySQL" in head
            or "<title>Forum not Found" in head or "Database Error</title>" in head
            or ("forumer" not in page.lower() and len(page) < 20000))


# -- hashed dumps ------------------------------------------------------------------------------

PERCENT = re.compile(rb"%([0-9A-Fa-f]{2})")
UNSAFE = set(':*?"<>|\\')


def query_hash(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8")).hexdigest()[:12]


def cdx_file_id(url: str) -> str:
    """The downloader's name for a snapshot of `url` (its sanitize_and_prepare_id)."""
    parts = urlsplit(url if re.match(r"(?i)https?://", url) else "http://" + url)
    raw = parts.path + ("?" + parts.query if parts.query else "")
    data = raw.removeprefix("/").encode("utf-8")
    while (decoded := PERCENT.sub(lambda m: bytes([int(m.group(1), 16)]), data)) != data:
        data = decoded
    text = re.sub(r"<!--+", "", data.decode("utf-8", "replace"))
    text = "".join(c for c in text if ord(c) >= 32)
    path, _, query = text.partition("?")
    if query:
        pre, dot, post = path.rpartition(".")
        path = f"{pre}__q{query_hash(query)}.{post}" if dot else f"{path}__q{query_hash(query)}"
    path = re.sub("/+", "/", path).removeprefix("/").removesuffix("/")  # (Ruby's split drops a trailing "")
    segments = []
    for segment in path.split("/") if path else []:
        segment = "".join(f"%{ord(c):02X}" if c in UNSAFE else c for c in segment)
        segments.append(segment.rstrip(" .") or "_")  # (Windows)
    return "/".join(segments).replace("<", "").replace(">", "")


def flat_file_id(name: str) -> str:
    """What the downloader would call a flat dump's root file (its URL's file id; the
    "#entry..." some names end with is the URL's fragment, not part of it)."""
    if name.startswith("index.php_"):
        return f"index__q{query_hash(name.split('_', 1)[1].partition('#')[0])}.php"
    return name


def _cdx(root: Path) -> list[tuple[str, str]]:
    """A dump's snapshots, (timestamp, url), once each (its listing repeats most)."""
    return sorted({(timestamp, url) for timestamp, url in json.loads((root / CDX).read_text(encoding="utf-8"))})


def _listed(root: Path) -> list[str] | None:
    listed = root / DOWNLOADED
    if not listed.is_file():
        return None
    return [line.strip() for line in listed.read_text(encoding="utf-8").splitlines() if line.strip()]


def _time(timestamp: str) -> datetime:
    return datetime.strptime(timestamp, "%Y%m%d%H%M%S")


def _latest(rows: list[tuple[str, str]]) -> dict[str, tuple[str, str]]:
    """file id -> (timestamp, url) of its latest snapshot."""
    latest: dict[str, tuple[str, str]] = {}
    for timestamp, url in rows:
        file_id = cdx_file_id(url)
        if file_id not in latest or latest[file_id][0] <= timestamp:
            latest[file_id] = (timestamp, url)
    return latest


@dataclass
class HashedDump:
    root: Path
    snapshots: dict[str, tuple[str, str]]  # file id -> (timestamp, url) of the latest snapshot
    downloaded: list[str]
    rows: list[tuple[str, str]]            # every snapshot: (timestamp, url)

    @classmethod
    def load(cls, root: Path) -> "HashedDump":
        rows = _cdx(root)
        snapshots = _latest(rows)
        return cls(root, snapshots, _listed(root) or list(snapshots), rows)

    def captured_at(self, file_id: str) -> datetime | None:
        """When the latest snapshot of a file id was captured."""
        snapshot = self.snapshots.get(file_id)
        return _time(snapshot[0]) if snapshot else None


@dataclass
class SnapshotDump:
    """A dump of every snapshot: <timestamp>/<file id>."""
    root: Path
    files: list[tuple[str, str, str]]      # (timestamp, file id, url) of each downloaded snapshot
    rows: list[tuple[str, str]]
    latest: dict[str, tuple[str, str]]
    unknown: list[str]                     # downloaded files no snapshot explains
    snapshot_count: int                    # the CDX's (timestamp, file id) pairs

    @classmethod
    def load(cls, root: Path) -> "SnapshotDump":
        rows = _cdx(root)
        by_time: dict[str, dict[str, str]] = {}  # timestamp -> file id -> url
        for timestamp, url in rows:
            by_time.setdefault(timestamp, {}).setdefault(cdx_file_id(url), url)
        files, unmatched = [], {}
        for line in _listed(root) or []:
            timestamp, _, file_id = line.partition("/")
            if (url := by_time.get(timestamp, {}).get(file_id)) is not None:
                files.append((timestamp, file_id, url))
            else:
                unmatched.setdefault(timestamp, []).append(file_id)
        # A name the downloader made differently (from a URL with odd bytes): the one snapshot of its
        # capture time that no file claimed.
        claimed = {(timestamp, file_id) for timestamp, file_id, _ in files}
        unknown = []
        for timestamp, file_ids in unmatched.items():
            left = [url for file_id, url in by_time.get(timestamp, {}).items() if (timestamp, file_id) not in claimed]
            if len(file_ids) == 1 and len(left) == 1:
                files.append((timestamp, file_ids[0], left[0]))
            else:
                unknown += [f"{timestamp}/{file_id}" for file_id in file_ids]
        snapshots = len({(timestamp, file_id) for timestamp, ids in by_time.items() for file_id in ids})
        return cls(root, sorted(files), rows, _latest(rows), unknown, snapshots)

    def captured_at(self, file_id: str) -> datetime | None:
        snapshot = self.latest.get(file_id)
        return _time(snapshot[0]) if snapshot else None


TIMESTAMP = re.compile(r"\d{14}")


def is_hashed(root: Path) -> bool:
    return (root / CDX).is_file() and not is_snapshots(root)


def is_snapshots(root: Path) -> bool:
    if not (root / CDX).is_file():
        return False
    listed = _listed(root)
    if listed:
        return TIMESTAMP.fullmatch(listed[0].partition("/")[0]) is not None
    return any(p.is_dir() and TIMESTAMP.fullmatch(p.name) for p in root.iterdir())


def _flat_files(root: Path) -> Iterator[tuple[str, DumpFile]]:
    """(file id, file) of a flat dump's pages."""
    for path in sorted(p for p in root.iterdir() if p.is_file()):
        query = query_of(path.name)
        yield flat_file_id(path.name), DumpFile(path, kind_of(query), query)
    archive = root / "a"
    if archive.is_dir():
        for path in sorted(archive.glob("*.html")):
            if ARCHIVE_TOPIC.search(path.name):
                yield f"a/{path.name}", DumpFile(path, "archive-topic", {})
        members = archive / "members" / "index.html"
        if members.is_file():
            yield "a/members", DumpFile(members, "archive-members", {})


def _page(folder: Path, file_id: str, url: str) -> tuple[Path, str, dict[str, str]] | None:
    """(path, kind, query) of a downloaded file worth parsing, saved under `folder`."""
    if "/" not in file_id and file_id.startswith("index__q"):
        query = parse_query(urlsplit(url).query)
        return folder / file_id, kind_of(query), query
    if file_id.startswith("a/") and file_id.count("/") == 1 and ARCHIVE_TOPIC.search(file_id):
        return folder / file_id, "archive-topic", {}
    if file_id == "a/members":  # saved as a folder: a/members/index.html
        return folder / file_id / "index.html", "archive-members", {}
    if file_id in ("", "index.php"):  # the board's index (its forums)
        return folder / (file_id or "index.html"), "other", {}
    return None


def _hashed_files(dump: HashedDump, restorer) -> Iterator[tuple[str, DumpFile]]:
    for file_id in sorted(dump.downloaded):
        if file_id and file_id in dump.snapshots and (page := _page(dump.root, file_id, dump.snapshots[file_id][1])):
            yield file_id, DumpFile(*page, f"{dump.root.name}/{file_id}", dump.captured_at(file_id), restorer)


def _snapshot_files(dump: SnapshotDump) -> Iterator[tuple[str, DumpFile]]:
    for timestamp, file_id, url in dump.files:
        if page := _page(dump.root / timestamp, file_id, url):
            source = f"{dump.root.name}/{timestamp}/{file_id}".removesuffix("/")
            yield file_id, DumpFile(*page, source, _time(timestamp))


def iter_dumps(roots: list[Path], report: Counter | None = None) -> list[DumpFile]:
    """Every page worth parsing of several dumps (of any layout), each capture once, oldest first.

    A capture that a snapshots dump and another dump both have is read from the
    snapshots dump; one that a flat and a hashed dump have, from the flat one, dated
    by the hashed one's CDX (all counted in `report`)."""
    from tapascrape.boards.metanet.relink import LinkRestorer

    report = report if report is not None else Counter()
    every = [SnapshotDump.load(root) for root in roots if is_snapshots(root)]
    hashed = [HashedDump.load(root) for root in roots if is_hashed(root)]
    files: list[DumpFile] = []
    snapshots: dict[str, list[DumpFile]] = {}  # file id -> the snapshots dumps' copies
    for dump in every:
        name = dump.root.name
        report[f"{name}: snapshots listed"] += dump.snapshot_count
        report[f"{name}: snapshots downloaded"] += len(dump.files)
        report[f"{name}: downloaded files no snapshot explains"] += len(dump.unknown)
        for file_id, file in _snapshot_files(dump):
            snapshots.setdefault(file_id, []).append(file)
            files.append(file)
    flat_ids: set[str] = set()
    for root in roots:
        if (root / CDX).is_file():
            continue
        for file_id, file in _flat_files(root):
            flat_ids.add(file_id)
            size = file.path.stat().st_size
            if any(copy.path.stat().st_size == size for copy in snapshots.get(file_id, ())):
                report[f"{root.name}: files read from the snapshots instead"] += 1
                continue
            dates = [d for dump in [*every, *hashed] if (d := dump.captured_at(file_id)) is not None]
            files.append(DumpFile(file.path, file.kind, file.query, captured_at=max(dates, default=None)))
    captures = {(file.captured_at, file_id) for file_id, copies in snapshots.items() for file in copies}
    for dump in hashed:
        restorer = LinkRestorer(url for _, url in dump.rows)
        for file_id, file in _hashed_files(dump, restorer):
            if (file.captured_at, file_id) in captures:
                report[f"{dump.root.name}: files read from the snapshots instead"] += 1
            elif file_id in flat_ids:
                report[f"{dump.root.name}: files read from the flat dump instead"] += 1
            else:
                files.append(file)
    files.sort(key=lambda f: (f.captured_at is not None, f.captured_at or datetime.min))
    return files


def read_ahead(files: Iterable[DumpFile], workers: int = 16) -> Iterator[tuple[DumpFile, bytes | None]]:
    """Each file with its bytes (None if unreadable), in order. Opening a file is slow on some
    disks (~0.1 s, without keeping them busy): reading several at once is several times faster."""
    def read(path: Path) -> bytes | None:
        try:
            return path.read_bytes()
        except OSError:
            return None

    with ThreadPoolExecutor(workers) as pool:
        pending: deque = deque()
        for file in files:
            pending.append((file, pool.submit(read, file.path)))
            if len(pending) >= 4 * workers:
                file, data = pending.popleft()
                yield file, data.result()
        while pending:
            file, data = pending.popleft()
            yield file, data.result()


