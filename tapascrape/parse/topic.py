"""Topic page (viewtopic.php): the posts shown on it."""

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from bs4 import BeautifulSoup


@dataclass
class WebPost:
    id: int
    index: int | None
    user_id: int | None
    author_name: str | None
    timestamp: datetime | None  # naive UTC, minute precision (the page has no seconds)
    content: str  # raw HTML of the post body, as the website renders it
    signature: str | None


def parse_topic_posts(html: str) -> list[WebPost]:
    soup = BeautifulSoup(html, "lxml")
    posts = []
    for div in soup.select("div.post[id^=p_]"):
        post_id = int(div["id"][2:])

        index = None
        if (label := div.select_one("p.author a.unread span")) and \
                (m := re.fullmatch(r"#(\d+)", label.get_text(strip=True))):
            index = int(m.group(1))

        user_id = None
        if (profile := div.select_one("dl.postprofile")) and profile.get("data-uid", "").isdigit():
            user_id = int(profile["data-uid"]) or None

        author = div.select_one(".thread-user-name [itemprop=name]") or div.select_one(".thread-user-name")
        timestamp = None
        if time := div.select_one("time[datetime]"):
            parsed = datetime.fromisoformat(time["datetime"])
            timestamp = parsed.astimezone(timezone.utc).replace(tzinfo=None)

        content = div.select_one("div.content")
        if content is not None:
            for marker in content.select('[data-tag="post_body_end"]'):
                marker.decompose()
        signature = div.select_one("div.signature")

        posts.append(WebPost(
            id=post_id,
            index=index,
            user_id=user_id,
            author_name=author.get_text(strip=True) if author else None,
            timestamp=timestamp,
            content=content.decode_contents().strip() if content is not None else "",
            signature=signature.decode_contents().strip() if signature is not None else None,
        ))
    return posts
