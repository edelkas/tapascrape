"""Images embedded in what only the forumer dump has, for `metanet images`.

The site shows a member's forumer signature when Tapatalk has none for them, or
doesn't have them at all (see site.MetanetSite.load_members), and the posts of
topics Tapatalk never got (load_posts); the images in those are looked for too,
with the ones of Tapatalk's signatures and posts.
"""

import logging
import sys
from collections.abc import Iterator

from tapascrape.content.images import post_texts, signature_texts
from tapascrape.db.base import Database
from tapascrape.parse.bbcode import html_to_bbcode

log = logging.getLogger(__name__)

DEEP_RECURSION = 20000  # as the site allows (site.DEEP_RECURSION)


def bbcode(html: str, what: str) -> str | None:
    """Forumer's rendered HTML -> BBCode, even nested deep; None if it's too deep still."""
    limit = sys.getrecursionlimit()
    try:
        return html_to_bbcode(html)
    except RecursionError:  # tags never closed, nested hundreds deep
        sys.setrecursionlimit(DEEP_RECURSION)
        try:
            return html_to_bbcode(html)
        except RecursionError:
            log.warning("a forumer %s is nested too deep to read; its images are left out", what)
            return None
    finally:
        sys.setrecursionlimit(limit)


def forumer_signature_texts(db: Database) -> Iterator[str]:
    """BBCode of the forumer signatures the site shows."""
    with_signature = {user_id for user_id, in db.query(
        "SELECT id FROM users WHERE COALESCE(NULLIF(signature_fixed, ''), NULLIF(signature_source, ''), "
        "NULLIF(signature, '')) IS NOT NULL")}
    for user_id, signature in db.query("SELECT user_id, signature FROM forumer_members "
                                       "WHERE signature IS NOT NULL AND signature <> ''"):
        if user_id not in with_signature and (text := bbcode(signature, "signature")):
            yield text


def forumer_post_texts(db: Database) -> Iterator[str]:
    """BBCode of the forumer posts the site shows: the ones of topics Tapatalk never got."""
    for html, in db.query("SELECT html FROM forumer_posts WHERE post_id IS NULL AND html IS NOT NULL "
                          "AND topic_id IS NOT NULL AND topic_id NOT IN (SELECT id FROM topics)"):
        if "<img" in html.lower() and (text := bbcode(html, "post")):
            yield text


def metanet_texts(db: Database, posts: bool = True) -> dict[str, Iterator[str]]:
    """Scope -> texts, for collect_images."""
    def chain(*sources):
        for source in sources:
            yield from source(db)

    texts = {"signatures": chain(signature_texts, forumer_signature_texts)}
    if posts:
        texts["posts"] = chain(post_texts, forumer_post_texts)
    return texts
