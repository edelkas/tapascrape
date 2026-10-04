"""The Wayback Machine dump of the Forumer board, as saved on disk.

Root files are the board's pages, named after their URL with "?" turned into
"_" (index.php_s=&showtopic=19141, index.php_act=ST&f=5&t=5031, sometimes with
"&amp;" or a session id). a/ is Forumer's "lite archive" (one page per 15
posts of a topic, plus a member index). Many pages are useless: login-only
pages answer with an error ("Board Message"), session redirects land on the
board index, and some are a parked domain's or a MySQL error page.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl

# act=... value -> kind of page we care about
ACT_KINDS = {"st": "topic", "sf": "forum", "attach": "attach", "members": "members",
             "legends": "emoticons", "print": "print"}


@dataclass(frozen=True)
class DumpFile:
    path: Path
    kind: str  # topic, profile, forum, attach, members, emoticons, archive-topic, archive-members, other
    query: dict[str, str]

    @property
    def name(self) -> str:
        return self.path.name

    def read(self) -> str:
        return decode(self.path.read_bytes())


def decode(data: bytes) -> str:
    """Pages are Windows-1252 (served as ISO-8859-1); latin-1 for the odd byte cp1252 lacks."""
    try:
        return data.decode("cp1252")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def query_of(name: str) -> dict[str, str]:
    """The query parameters of a root file's URL (keys lowercased)."""
    query = name.split("_", 1)[1] if name.startswith("index.php_") else ""
    query = query.replace("&amp;", "&")
    return {k.lower(): v for k, v in parse_qsl(query, keep_blank_values=True)}


def kind_of(query: dict[str, str]) -> str:
    if "showtopic" in query:
        return "topic"
    if "showuser" in query:
        return "profile"
    if "showforum" in query:
        return "forum"
    return ACT_KINDS.get(query.get("act", "").lower(), "other")


def iter_dump(root: Path) -> Iterator[DumpFile]:
    """Every page of the dump worth parsing, root pages first."""
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


ARCHIVE_TOPIC = re.compile(r"_post(\d+)(?:-(\d+))?\.html$")


def archive_position(name: str) -> tuple[int, int] | None:
    """(topic id, offset of the page's first post) of a lite-archive page name."""
    match = ARCHIVE_TOPIC.search(name)
    return (int(match.group(1)), int(match.group(2) or 0)) if match else None


def is_error(page: str) -> bool:
    """A login-required/error page, a MySQL failure or a parked-domain answer."""
    head = page[:6000]
    return ("<title>Board Message</title>" in head or "Can't connect to local MySQL" in head
            or ("forumer" not in page.lower() and len(page) < 20000))
