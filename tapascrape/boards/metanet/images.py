"""Images embedded in forumer-era signatures, for `metanet images`.

The site shows a member's forumer signature when Tapatalk has none for them, or
doesn't have them at all (see site.MetanetSite.load_members); the images in
those are looked for too, with the ones of Tapatalk's signatures.
"""

import logging
from collections.abc import Iterator

from tapascrape.content.images import signature_texts
from tapascrape.db.base import Database
from tapascrape.parse.bbcode import html_to_bbcode

log = logging.getLogger(__name__)


def forumer_signature_texts(db: Database) -> Iterator[str]:
    """BBCode of the forumer signatures the site shows."""
    with_signature = {user_id for user_id, in db.query(
        "SELECT id FROM users WHERE COALESCE(NULLIF(signature_fixed, ''), NULLIF(signature_source, ''), "
        "NULLIF(signature, '')) IS NOT NULL")}
    for user_id, signature in db.query("SELECT user_id, signature FROM forumer_members "
                                       "WHERE signature IS NOT NULL AND signature <> ''"):
        if user_id in with_signature:
            continue
        try:
            yield html_to_bbcode(signature)
        except RecursionError:  # tags never closed, nested hundreds deep
            log.warning("a forumer signature is nested too deep to read; its images are left out")


def metanet_signature_texts(db: Database) -> Iterator[str]:
    yield from signature_texts(db)
    yield from forumer_signature_texts(db)
