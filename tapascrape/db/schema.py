"""Backend-neutral table definitions. Adapters render them to DDL.

IDs are the board's own IDs (no auto-increment), except those of avatars, smilies and
attachments.
"""

from dataclasses import dataclass

# Neutral column types; each adapter maps them to its own SQL types.
INT = "int"          # 64-bit integer
BOOL = "bool"
TEXT = "text"        # short-ish text (names, titles)
LONGTEXT = "longtext"  # HTML bodies
DATETIME = "datetime"  # naive UTC
BLOB = "blob"


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    nullable: bool = True
    autoincrement: bool = False


@dataclass(frozen=True)
class Index:
    name: str
    columns: tuple[str, ...]


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple[Column, ...]
    primary_key: tuple[str, ...]
    indexes: tuple[Index, ...] = ()

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]


FORUMS = Table("forums", (
    Column("id", INT, nullable=False),
    Column("parent_id", INT),
    Column("name", TEXT, nullable=False),
    Column("description", LONGTEXT),
    Column("last_post_id", INT),
    Column("post_count", INT),
    Column("view_count", INT),
), primary_key=("id",), indexes=(Index("ix_forums_parent", ("parent_id",)),))

TOPICS = Table("topics", (
    Column("id", INT, nullable=False),
    Column("forum_id", INT, nullable=False),
    Column("user_id", INT),
    Column("name", TEXT, nullable=False),
    Column("stickied", BOOL, nullable=False),
    Column("locked", BOOL, nullable=False),
    Column("created_at", DATETIME),
    Column("post_count", INT),
    Column("view_count", INT),
    Column("last_post_id", INT),
), primary_key=("id",), indexes=(Index("ix_topics_forum", ("forum_id",)),))

POSTS = Table("posts", (
    Column("id", INT, nullable=False),
    Column("topic_id", INT, nullable=False),
    Column("user_id", INT),
    Column("index", INT, nullable=False),
    Column("timestamp", DATETIME),
    Column("content", LONGTEXT),  # HTML as returned by the API (or the website, see enrich)
    Column("source", LONGTEXT),  # original BBCode source, when available (see crawl.sources)
    Column("source_fixed", LONGTEXT),  # source with migration leftovers repaired (content.fix)
), primary_key=("id",), indexes=(
    Index("ix_posts_topic", ("topic_id", "index")),
    Index("ix_posts_user", ("user_id",)),
))

USERS = Table("users", (
    Column("id", INT, nullable=False),
    Column("name", TEXT, nullable=False),
    Column("rank", TEXT),
    Column("joined_at", DATETIME),
    Column("last_active_at", DATETIME),
    Column("post_count", INT),
    Column("signature", LONGTEXT),
    Column("signature_source", LONGTEXT),  # BBCode rebuilt from the signature HTML (content.fix)
    Column("signature_fixed", LONGTEXT),   # signature_source with the post fixes applied
    Column("avatar_url", TEXT),
    Column("avatar_id", INT),
), primary_key=("id",))

AVATARS = Table("avatars", (
    Column("id", INT, nullable=False, autoincrement=True),
    Column("user_id", INT, nullable=False),
    Column("data", BLOB, nullable=False),
), primary_key=("id",), indexes=(Index("ix_avatars_user", ("user_id",)),))

GROUPS = Table("groups", (
    Column("id", INT, nullable=False),
    Column("name", TEXT),
), primary_key=("id",))

GROUP_USERS = Table("group_users", (
    Column("group_id", INT, nullable=False),
    Column("user_id", INT, nullable=False),
), primary_key=("group_id", "user_id"), indexes=(Index("ix_group_users_user", ("user_id",)),))

# Smileys that posts embedded as images from long-dead hosts (Yuku, forumer).
# Ids are ours (stable once given); fixed sources refer to them as [ts:smiley=ID].
SMILIES = Table("smilies", (
    Column("id", INT, nullable=False),
    Column("url", TEXT, nullable=False),  # as posts used it, normalized (see content.smilies)
    Column("name", TEXT),                 # file name without extension, e.g. "tongue"
    Column("host", TEXT),                 # "yuku", "forumer" or "other"
    Column("uses", INT),                  # occurrences in post sources
    Column("content_type", TEXT),
    Column("data", BLOB),                 # NULL until recovered
    Column("recovered_from", TEXT),       # where `data` came from (e.g. a Wayback capture)
), primary_key=("id",))

# Files uploaded to the old forumer (Invision) board, which posts link or embed.
# Ids are ours (stable once given); fixed sources refer to them with
# [ts:attachment=ID], [ts:attachment-image=ID], [ts:attachment-link=ID]...
ATTACHMENTS = Table("attachments", (
    Column("id", INT, nullable=False),
    Column("kind", TEXT, nullable=False),  # "upload" (a file name) or "attach-id" (act=Attach&id=N)
    Column("url", TEXT, nullable=False),   # canonical original URL
    Column("name", TEXT),                  # file name, e.g. "post-10-1081445810.txt"
    Column("old_member_id", INT),          # forumer member id, from post-<member>-<time>.<ext>
    Column("uploaded_at", DATETIME),       # from the same name (unix time)
    Column("old_attach_id", INT),          # forumer attachment id, for "attach-id"
    Column("first_post_id", INT),          # first post referencing it (NULL: only signatures)
    Column("uses", INT),                   # references in posts and signatures
    Column("content_type", TEXT),
    Column("size", INT),
    Column("data", BLOB),                  # NULL until recovered
    Column("recovered_from", TEXT),        # where `data` came from (e.g. a Wayback capture)
), primary_key=("id",))

# Bookkeeping for resumable crawls (e.g. "topic:6364" -> "done").
CRAWL_STATE = Table("crawl_state", (
    Column("key", TEXT, nullable=False),
    Column("value", TEXT),
), primary_key=("key",))

TABLES = (FORUMS, TOPICS, POSTS, USERS, AVATARS, GROUPS, GROUP_USERS, SMILIES, ATTACHMENTS,
          CRAWL_STATE)
TABLES_BY_NAME = {t.name: t for t in TABLES}
