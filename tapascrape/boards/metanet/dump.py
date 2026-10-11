"""The Wayback Machine dumps of the Forumer board, as saved on disk.

Two layouts, one per version of the downloader that made them:

- flat (the 2019 dump): root files are the board's pages, named after their
  URL with "?" turned into "_" (index.php_s=&showtopic=19141,
  index.php_act=ST&f=5&t=5031, sometimes with "&amp;" or a session id).
- hashed (StrawberryMaster's wayback-machine-downloader): a page's query is
  hashed into its name (index.php?Q -> index__q<sha256(Q)[:12]>.php). Its
  .cdx.json lists every snapshot ([timestamp, url]); each name is the latest
  snapshot of its URLs, so the name gives back the URL and the capture date
  (cdx_file_id repeats how the downloader names them). It also rewrote the
  board's own links in the pages the same way, and dropped their non-ASCII
  bytes: relink.py restores the links.

Both have a/, Forumer's "lite archive" (one page per 15 posts of a topic, plus
a member index). Many pages are useless: login-only pages answer with an error
("Board Message"), session redirects land on the board index, and some are a
parked domain's or a MySQL error page.

iter_dumps reads several dumps as one: a page both have (the same URL) is read
from the flat one, whose links are intact, and the hashed dumps' CDX dates the
flat dump's pages too. Pages come oldest capture first (undated first), so
whatever merges them can let the latest win.
"""

import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

# act=... value -> kind of page we care about
ACT_KINDS = {"st": "topic", "sf": "forum", "attach": "attach", "members": "members",
             "legends": "emoticons", "print": "print"}
CDX = ".cdx.json"
DOWNLOADED = ".downloaded.txt"


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

    def read(self) -> str:
        data = self.path.read_bytes()
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


def newer(old, new, lossy: bool):
    """`new`, a later copy's value, unless it's missing or, from a lossy copy, only lacks the
    characters it dropped and the links it couldn't restore."""
    if new is None or new == "":
        return old
    if lossy and isinstance(old, str) and isinstance(new, str) and _lossy_form(old) == _lossy_form(new):
        return old
    return new


NON_ASCII = re.compile(r"[^\x00-\x7f]")
LINK_ATTRIBUTE = re.compile(r"""\b(href|src|action)=(['"])[^'"]*\2""")


def _lossy_form(value: str) -> str:
    return LINK_ATTRIBUTE.sub(r"\1=", NON_ASCII.sub("", value))


def is_error(page: str) -> bool:
    """A login-required/error page, a MySQL failure or a parked-domain answer."""
    head = page[:6000]
    return ("<title>Board Message</title>" in head or "Can't connect to local MySQL" in head
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
    """What the downloader would call a flat dump's root file (its URL's file id)."""
    if name.startswith("index.php_"):
        return f"index__q{query_hash(name.split('_', 1)[1])}.php"
    return name


@dataclass
class HashedDump:
    root: Path
    snapshots: dict[str, tuple[str, str]]  # file id -> (timestamp, url) of the latest snapshot
    downloaded: list[str]
    rows: list[tuple[str, str]]            # every snapshot: (timestamp, url)

    @classmethod
    def load(cls, root: Path) -> "HashedDump":
        snapshots: dict[str, tuple[str, str]] = {}
        rows = [tuple(row) for row in json.loads((root / CDX).read_text(encoding="utf-8"))]
        for timestamp, url in rows:
            file_id = cdx_file_id(url)
            if file_id not in snapshots or snapshots[file_id][0] <= timestamp:
                snapshots[file_id] = (timestamp, url)
        listed = root / DOWNLOADED
        downloaded = ([line.strip() for line in listed.read_text(encoding="utf-8").splitlines() if line.strip()]
                      if listed.is_file() else list(snapshots))
        return cls(root, snapshots, downloaded, rows)

    def captured_at(self, file_id: str) -> datetime | None:
        snapshot = self.snapshots.get(file_id)
        return datetime.strptime(snapshot[0], "%Y%m%d%H%M%S") if snapshot else None


def is_hashed(root: Path) -> bool:
    return (root / CDX).is_file()


def _flat_files(root: Path) -> Iterator[DumpFile]:
    for path in sorted(p for p in root.iterdir() if p.is_file()):
        query = query_of(path.name)
        yield DumpFile(path, kind_of(query), query)
    archive = root / "a"
    if archive.is_dir():
        for path in sorted(archive.glob("*.html")):
            if ARCHIVE_TOPIC.search(path.name):
                yield DumpFile(path, "archive-topic", {})
        members = archive / "members" / "index.html"
        if members.is_file():
            yield DumpFile(members, "archive-members", {})


def _hashed_files(dump: HashedDump, restorer) -> Iterator[DumpFile]:
    prefix = dump.root.name
    for file_id in sorted(dump.downloaded):
        captured_at = dump.captured_at(file_id)
        source = f"{prefix}/{file_id}"
        if "/" not in file_id and file_id.startswith("index__q") and file_id in dump.snapshots:
            query = parse_query(urlsplit(dump.snapshots[file_id][1]).query)
            yield DumpFile(dump.root / file_id, kind_of(query), query, source, captured_at, restorer)
        elif file_id.startswith("a/") and file_id.count("/") == 1 and ARCHIVE_TOPIC.search(file_id):
            yield DumpFile(dump.root / file_id, "archive-topic", {}, source, captured_at, restorer)
        elif file_id == "a/members":  # saved as a folder: a/members/index.html
            yield DumpFile(dump.root / file_id / "index.html", "archive-members", {}, source, captured_at,
                           restorer)


def iter_dumps(roots: list[Path], report: Counter | None = None) -> Iterator[DumpFile]:
    """Every page worth parsing of several dumps (of either layout), oldest capture first.

    A page that a flat dump and a hashed dump both have is read from the flat
    one, dated by the hashed one's CDX (counted in `report`)."""
    from tapascrape.boards.metanet.relink import LinkRestorer

    hashed = [HashedDump.load(root) for root in roots if is_hashed(root)]
    files: list[DumpFile] = []
    flat_ids: set[str] = set()
    for root in roots:
        if is_hashed(root):
            continue
        for file in _flat_files(root):
            file_id = flat_file_id(file.path.name) if file.path.parent == root else \
                file.path.relative_to(root).as_posix()
            flat_ids.add(file_id)
            dates = [d for dump in hashed if (d := dump.captured_at(file_id)) is not None]
            files.append(DumpFile(file.path, file.kind, file.query, captured_at=max(dates, default=None)))
    for dump in hashed:
        restorer = LinkRestorer(url for _, url in dump.rows)
        for file in _hashed_files(dump, restorer):
            if file.source.split("/", 1)[1] not in flat_ids:
                files.append(file)
            elif report is not None:
                report["files read from the flat dump instead"] += 1
    files.sort(key=lambda f: (f.captured_at is not None, f.captured_at or datetime.min))
    return iter(files)
