"""The generic quote matching (content.quotes), knowing the names of the Forumer era.

Quotes name authors as they were called when quoted: forumer members' names
(which give the accounts Tapatalk only knows by their id their real name) and
the authors forumer showed on posts (guests among them).
"""

from collections import Counter, defaultdict

from tapascrape.boards.metanet.schema import TABLES
from tapascrape.content.quotes import link_quotes
from tapascrape.db.base import Database


def forumer_names(db: Database) -> tuple[dict[int, set[str]], dict[int, str]]:
    """(user id -> forumer names, post id -> author forumer showed)."""
    aliases: dict[int, set[str]] = defaultdict(set)
    for user_id, name in db.query("SELECT user_id, name FROM forumer_members "
                                  "WHERE user_id IS NOT NULL AND name IS NOT NULL"):
        aliases[user_id].add(name)
    authors = dict(db.query("SELECT post_id, author FROM forumer_archive_posts "
                            "WHERE post_id IS NOT NULL AND author IS NOT NULL"))
    authors.update(db.query("SELECT post_id, author FROM forumer_posts "
                            "WHERE post_id IS NOT NULL AND author IS NOT NULL"))
    return aliases, authors


def link_metanet_quotes(db: Database) -> Counter:
    db.create_schema(TABLES)
    aliases, authors = forumer_names(db)
    return link_quotes(db, aliases, authors)
