"""Backend-neutral table definitions. Adapters render them to DDL.

IDs are the board's own IDs (no auto-increment), except avatars.id.
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
    Column("content", LONGTEXT),
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

# Bookkeeping for resumable crawls (e.g. "topic:6364" -> "done").
CRAWL_STATE = Table("crawl_state", (
    Column("key", TEXT, nullable=False),
    Column("value", TEXT),
), primary_key=("key",))

TABLES = (FORUMS, TOPICS, POSTS, USERS, AVATARS, GROUPS, GROUP_USERS, CRAWL_STATE)
TABLES_BY_NAME = {t.name: t for t in TABLES}
