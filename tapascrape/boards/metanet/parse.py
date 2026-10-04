"""Parsers for the pages of the old Forumer board (Invision Power Board 1.x).

Pages come from several skins (2005, 2006-07, the 2008 "We're Back!" one),
which differ in details: "Posted:" vs "Posted on", "Oct 2 2005, 04:08 AM" vs
"October 02, 2005 04:08 am", "Joined: 22-August 05" vs "Joined: August 22,
2005". Everything here works on the page text and returns plain dataclasses.

Displayed times are UTC: they match the Tapatalk timestamps to the minute.
"""

import html
import json
import re
from dataclasses import dataclass, field
from datetime import datetime

DATETIME_FORMATS = ("%B %d, %Y %I:%M %p", "%b %d %Y, %I:%M %p")
DAY_FORMATS = ("%B %d, %Y", "%d-%B %y", "%b %d %Y", "%m-%d-%Y", "%d %B %Y", "%B %d %Y")


def text(fragment: str | None) -> str | None:
    """Plain text of an HTML fragment, or None when there's none."""
    if fragment is None:
        return None
    fragment = re.sub(r"<script.*?</script>", "", fragment, flags=re.S | re.I)
    value = html.unescape(re.sub(r"<[^>]+>", "", fragment)).replace("\xa0", " ").strip()
    return value or None


def parse_datetime(value: str | None) -> datetime | None:
    """A post time; None for relative ones ("Today, 04:08 AM") or anything unknown."""
    value = " ".join((value or "").split())
    for fmt in DATETIME_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    return None


def parse_day(value: str | None) -> datetime | None:
    value = " ".join((value or "").split())
    for fmt in DAY_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    return None


def number(value: str | None) -> int | None:
    match = re.search(r"\d[\d,]*", value or "")
    return int(match.group(0).replace(",", "")) if match else None


# -- topic pages (showtopic / act=ST) -------------------------------------------

@dataclass
class Author:
    member_id: int | None  # None: a guest (or a deleted member)
    name: str | None
    avatar_url: str | None = None
    title: str | None = None
    group: str | None = None
    post_count: int | None = None
    joined_at: datetime | None = None
    country: str | None = None


@dataclass
class Attached:
    kind: str                # "file" (act=Attach download) or "image" (shown inline)
    attach_id: int | None    # act=Attach&id=N; forumer used the post's own id
    name: str | None = None  # original file name, for files
    downloads: int | None = None
    url: str | None = None   # the uploaded file, for images


@dataclass
class Post:
    id: int
    topic_id: int | None
    forum_id: int | None
    author: Author
    posted: str | None          # as displayed
    posted_at: datetime | None
    html: str                   # the post body, without the edit note and attachment box
    signature: str | None = None
    edited_by: str | None = None
    edited_at: datetime | None = None
    attachments: list[Attached] = field(default_factory=list)


@dataclass
class TopicPage:
    topic_id: int | None
    forum_id: int | None
    title: str | None
    description: str | None
    poll: dict | None
    posts: list[Post]


MSG_START = re.compile(r"<!--Begin Msg Number (\d+)-->")
QUOTE_LINK = re.compile(r"act=Post&(?:amp;)?CODE=06&(?:amp;)?f=(\d+)&(?:amp;)?t=(\d+)")
TOPIC_LINK = re.compile(r"act=(?:Track|Forward|Print)&(?:amp;)?(?:client=\w+&(?:amp;)?)?"
                        r"f=(\d+)&(?:amp;)?t=(\d+)")
MEMBER = re.compile(r"<span class='normalname'><a href=['\"][^'\"]*showuser=(\d+)['\"]>(.*?)</a>", re.S)
GUEST = re.compile(r"<span class='unreg'>(.*?)</span>", re.S)
POSTED = re.compile(r"Posted(?: on)?:?</a></b>\s*(.*?)</span>", re.S)
BODY = re.compile(r"<!-- THE POST \d+ -->\s*<div class='postcolor'>(.*?)</div>\s*"
                  r"(?=<!--TEMPLATE: skin_global, Template Part: signature_separator-->|<!-- THE POST -->)",
                  re.S)
SIGNATURE = re.compile(r"<div class='signature'>(.*?)</div>\s*<!-- THE POST -->", re.S)
EDIT_NOTE = re.compile(r"<span class='edit'>This post has been edited by <b>(.*?)</b> on (.*?)</span>", re.S)
ATTACH_BOX = "<!--TEMPLATE: skin_topic, Template Part: Show_attachments"
ATTACH_FILE = re.compile(r"act=Attach&(?:amp;)?type=post&(?:amp;)?id=(\d+)['\"][^>]*>([^<]+)</a>")
ATTACH_IMAGE = re.compile(r"<img src='([^']+)' class='attach'")
TOPIC_HEADER = re.compile(r"nav_m\.gif'[^/]*/>(?:&nbsp;)?(?:<font[^>]*>)?<b>(.*?)</b>(.*?)</(?:font|div|td)>",
                          re.S)


def topic_page(page: str) -> TopicPage:
    starts = list(MSG_START.finditer(page))
    posts = []
    for i, start in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(page)
        if (post := _post(int(start.group(1)), page[start.end():end])) is not None:
            posts.append(post)
    topic_id = forum_id = None
    if match := TOPIC_LINK.search(page) or QUOTE_LINK.search(page):
        forum_id, topic_id = int(match.group(1)), int(match.group(2))
    title = description = None
    if starts and (match := TOPIC_HEADER.search(page, 0, starts[0].start())):
        title = text(match.group(1))
        description = text(match.group(2).lstrip(" ,"))
    return TopicPage(topic_id, forum_id, title, description, poll(page), posts)


def _post(post_id: int, block: str) -> Post | None:
    body = BODY.search(block)
    if body is None:
        return None
    content = body.group(1)
    attachments = []
    if (box := content.find(ATTACH_BOX)) >= 0:
        content, box_html = content[:box], content[box:]
        downloads = [number(d) for d in re.findall(r"Number of downloads: ([\d,]+)", box_html)]
        for i, (attach_id, name) in enumerate(ATTACH_FILE.findall(box_html)):
            attachments.append(Attached("file", int(attach_id), text(name),
                                        downloads[i] if i < len(downloads) else None))
        for url in ATTACH_IMAGE.findall(box_html):
            attachments.append(Attached("image", None, url=html.unescape(url)))
    edited_by = edited_at = None
    if edit := EDIT_NOTE.search(content):
        edited_by, edited_at = text(edit.group(1)), parse_datetime(text(edit.group(2)))
        content = content[:edit.start()] + content[edit.end():]
    posted = text(m.group(1)) if (m := POSTED.search(block)) else None
    topic_id = forum_id = None
    if m := QUOTE_LINK.search(block):
        forum_id, topic_id = int(m.group(1)), int(m.group(2))
    signature = m.group(1).strip() if (m := SIGNATURE.search(block)) else None
    return Post(post_id, topic_id, forum_id, _author(block), posted, parse_datetime(posted),
                content.strip(), signature or None, edited_by, edited_at, attachments)


def _author(block: str) -> Author:
    if m := MEMBER.search(block):
        author = Author(int(m.group(1)) or None, text(m.group(2)))
    else:
        author = Author(None, text(m.group(1)) if (m := GUEST.search(block)) else None)
    details = re.search(r"<span class='postdetails'><a href=['\"][^'\"]*showuser=\d+['\"]>(.*?)</a>(.*?)"
                        r"<!--\$ author\[field_1\]-->", block, re.S)
    if details is None or author.member_id is None:
        return author
    if avatar := re.search(r"<img src='([^']+)'", details.group(1)):
        author.avatar_url = html.unescape(avatar.group(1))
    rest = details.group(2)
    if title := re.match(r"\s*(?:<br />\s*)*(.*?)<br />", rest, re.S):
        author.title = text(title.group(1))
    if m := re.search(r"Group: (.*?)<br", rest):
        author.group = text(m.group(1))
    if m := re.search(r"Posts: ([\d,]+)", rest):
        author.post_count = number(m.group(1))
    if m := re.search(r"Joined: (.*?)<br", rest):
        author.joined_at = parse_day(text(m.group(1)))
    if (m := re.search(r'if\("(.*?)"==""\)', rest)) and m.group(1) not in ("", "--Select--"):
        author.country = html.unescape(m.group(1))
    return author


def poll(page: str) -> dict | None:
    """{"question", "options": [[option, votes]...], "votes"} of a topic's poll, if shown."""
    start = page.find("Template Part: poll_header")
    if start < 0:
        return None
    end = page.find("Template Part: ShowPoll_footer", start)
    section = page[start:end if end >= 0 else len(page)]
    question = re.search(r"<td colspan='3' align='center'><b>(.*?)</b></td>", section, re.S)
    options = [[text(option), int(votes)] for option, votes in re.findall(
        r"<td class='row1'>(.*?)</td>\s*<td class='row1'> \[ <b>(\d+)</b> \] </td>", section, re.S)]
    if question is None and not options:
        return None
    total = re.search(r"Total Votes: (\d+)", section)
    return {"question": text(question.group(1)) if question else None, "options": options,
            "votes": int(total.group(1)) if total else None}


def poll_json(value: dict | None) -> str | None:
    return json.dumps(value, ensure_ascii=False) if value else None


# -- profiles (showuser) ------------------------------------------------------------

PROFILE_FIELDS = {  # label (lowercased) -> our name
    "joined": "joined_at", "total cumulative posts": "post_count", "birthday": "birthday",
    "location": "location", "specific location": "specific_location", "interests": "interests",
    "home page": "website", "website": "website", "msn identity": "msn", "aim name": "aim",
    "aol/aim username": "aim", "yahoo identity": "yahoo", "yahoo! identity": "yahoo",
    "icq number": "icq", "integrity messenger": "integrity", "member group": "group",
    "member title": "title", "avatar": "avatar_url", "signature": "signature",
}


def profile(page: str) -> dict | None:
    """A member's profile: {"member_id", "name", and the PROFILE_FIELDS found}."""
    name = re.search(r'<div id="profilename">(.*?)</div>', page, re.S)
    member = re.search(r"(?:mid|MID)=(\d+)", page[name.end():] if name else "")
    if name is None or member is None:
        return None
    found = {"member_id": int(member.group(1)), "name": text(name.group(1))}
    for label, value in re.findall(r"<td class=\"row3\"[^>]*><b>(.*?)</b></td>\s*<td[^>]*>(.*?)</td>",
                                   page, re.S):
        key = PROFILE_FIELDS.get((text(label) or "").lower())
        if key is None or key in found:
            continue
        if key == "signature":
            found[key] = value.strip() or None
        elif key == "avatar_url":
            if m := re.search(r"<img src='([^']+)'", value):
                found[key] = html.unescape(m.group(1))
        else:
            plain = text(value)
            if plain and plain != "No Information":
                found[key] = plain
    if "joined_at" in found:
        found["joined_at"] = parse_day(found["joined_at"])
    if "post_count" in found:
        found["post_count"] = number(found["post_count"])
    return found


# -- member lists ---------------------------------------------------------------------

def archive_members(page: str) -> list[tuple[int, str]]:
    """(old id, name) of every member, from the lite archive's member index (a/members/)."""
    return [(int(member_id), text(name) or "")
            for member_id, name in re.findall(r"user_[^'\"]*?_(\d+)\.html['\"]>(.*?)</a>", page)]


def member_list(page: str) -> list[dict]:
    """Rows of an act=Members page."""
    rows = re.findall(r"showuser=(\d+)\">(.*?)</a></strong></td>\s*<td[^>]*>.*?</td>\s*"
                      r"<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>([\d,]+)</td>", page, re.S)
    return [{"member_id": int(member_id), "name": text(name), "group": text(group),
             "joined_at": parse_day(text(joined)), "post_count": number(posts)}
            for member_id, name, group, joined, posts in rows]


# -- forum listings (showforum / act=SF) --------------------------------------------

TOPIC_ROW = re.compile(
    r"showtopic=(\d+)['\"][^>]*?title=['\"]This topic was started: ([^'\"]*)['\"][^>]*>(.*?)</a>"
    r"((?:(?!This topic was started).)*?)<span class='desc'>(.*?)</span>", re.S)


def forum_topics(page: str) -> list[dict]:
    """Topics listed on a forum page, with their descriptions."""
    found = []
    for match in TOPIC_ROW.finditer(page):
        before = page[max(0, match.start() - 300):match.start()]
        found.append({"topic_id": int(match.group(1)), "started_at": parse_datetime(match.group(2)),
                      "title": text(match.group(3)), "description": text(match.group(5)),
                      "pinned": "Pinned:" in before.rsplit("<td", 1)[-1]})
    return found


# -- lite archive topics (a/<slug>_post<topic>[-<start>].html) -----------------------

ARCHIVE_POST = re.compile(r"<div class='phead'><b>(.*?)</b>- (\d\d-\d\d-\d{4})</div>"
                          r"<div class='postshell'><div class='postcolor'>(.*?)(?:<br>)?\s*"
                          r"(?=<script|</div></div>)", re.S)


def archive_posts(page: str) -> list[tuple[str | None, datetime | None, str]]:
    """(author, day, html) of the posts on a lite-archive page, in order."""
    return [(text(author), parse_day(day), body.strip())
            for author, day, body in ARCHIVE_POST.findall(page)]


# -- emoticons ---------------------------------------------------------------------------

def emoticons(page: str) -> list[tuple[str, str]]:
    """(code, image URL) pairs: from the emoticon legend and from posts' <!--emo&CODE--> marks."""
    pairs = re.findall(r"add_smilie\(\"(.*?)\"\)'><img src='([^']+)'", page)
    pairs += re.findall(r"<!--emo&(.*?)--><img src='([^']+)'", page)
    return [(html.unescape(code), html.unescape(url)) for code, url in pairs]
