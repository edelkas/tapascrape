"""forumer_* tables: what the Forumer dump holds, keyed by the old board's ids.

They're only created by the `metanet` commands. Columns pointing at the
Tapatalk tables (user_id, post_id, attachment_id) are filled in by `link`,
with `match` saying how each was found.
"""

from tapascrape.db.schema import (BLOB, BOOL, DATETIME, INT, LONGTEXT, TEXT, Column, Index, Table,
                                  register)

FORUMER_MEMBERS = Table("forumer_members", (
    Column("old_id", INT, nullable=False),  # forumer's member id ("Member No.")
    Column("name", TEXT),
    Column("user_id", INT),                 # -> users.id
    Column("match", TEXT),                  # "name" or "position" (see link.link_members)
    Column("group_name", TEXT),
    Column("title", TEXT),                  # member title, e.g. "Advanced Member"
    Column("joined_at", DATETIME),          # day only
    Column("post_count", INT),              # as last seen
    Column("avatar_url", TEXT),
    Column("country", TEXT),                # the flag field of the 2006-07 skin
    Column("signature", LONGTEXT),          # HTML, as last seen
    # Profile fields. Personal data: keep it out of anything published.
    Column("birthday", TEXT),
    Column("location", TEXT),
    Column("specific_location", TEXT),
    Column("interests", LONGTEXT),
    Column("website", TEXT),
    Column("msn", TEXT),
    Column("aim", TEXT),
    Column("yahoo", TEXT),
    Column("icq", TEXT),
    Column("integrity", TEXT),
), primary_key=("old_id",), indexes=(Index("ix_forumer_members_user", ("user_id",)),))

FORUMER_FORUMS = Table("forumer_forums", (
    Column("id", INT, nullable=False),      # forum ids survived the migrations: = forums.id
    Column("name", TEXT),
    Column("description", TEXT),
    Column("parent_id", INT),               # the forum it's in; NULL: right in its category
    Column("category", TEXT),               # forumer's category (Tapatalk made them forums)
    Column("in_tapatalk", BOOL),            # whether `forums` has it
), primary_key=("id",))

FORUMER_TOPICS = Table("forumer_topics", (
    Column("id", INT, nullable=False),      # topic ids survived the migrations: = topics.id
    Column("forum_id", INT),
    Column("title", TEXT),
    Column("description", TEXT),            # IPB's topic description, which Tapatalk lacks
    Column("started_at", DATETIME),
    Column("pinned", BOOL),
    Column("has_poll", BOOL),               # NULL: never seen listed or opened (see polls)
    Column("in_tapatalk", BOOL),            # whether `topics` has it (some never made it)
), primary_key=("id",), dropped=("poll",))  # the poll's JSON, now in forumer_polls

# The polls the dump shows the results of: like `polls`, with the most voted copy kept.
FORUMER_POLLS = Table("forumer_polls", (
    Column("topic_id", INT, nullable=False),  # = forumer_topics.id
    Column("title", TEXT),                    # the question
    Column("vote_count", INT),                # "Total Votes"
    Column("option_count", INT),
    Column("max_options", INT),               # NULL: forumer's pages don't say
    Column("options", LONGTEXT),              # JSON: [{"text": ..., "votes": n}, ...] in order
    Column("source_file", TEXT),              # dump file it was read from
), primary_key=("topic_id",))

FORUMER_POSTS = Table("forumer_posts", (
    Column("id", INT, nullable=False),      # forumer's post id (old_post= in sentinels)
    Column("topic_id", INT),
    Column("forum_id", INT),
    Column("post_id", INT),                 # -> posts.id
    Column("match", TEXT),                  # "time", "time+author", "time+order", "interpolated"
    Column("member_id", INT),               # -> forumer_members.old_id; NULL for guests
    Column("author", TEXT),
    Column("posted", TEXT),                 # as displayed
    Column("posted_at", DATETIME),          # minute precision, UTC
    Column("html", LONGTEXT),               # rendered body; NULL for rows only known by id
    Column("edited_by", TEXT),
    Column("edited_at", DATETIME),
    Column("source_file", TEXT),            # dump file it was read from
), primary_key=("id",), indexes=(
    Index("ix_forumer_posts_topic", ("topic_id",)),
    Index("ix_forumer_posts_post", ("post_id",)),
))

FORUMER_ATTACHMENTS = Table("forumer_attachments", (
    Column("old_post_id", INT, nullable=False),
    Column("ref", TEXT, nullable=False),    # the attach id (files) or the uploaded file's URL (images)
    Column("kind", TEXT),                   # "file" or "image"
    Column("name", TEXT),                   # original file name
    Column("downloads", INT),
    Column("attachment_id", INT),           # -> attachments.id
    Column("content_type", TEXT),
    Column("data", BLOB),                   # the file, when the dump has it
    Column("source_file", TEXT),
), primary_key=("old_post_id", "ref"))

FORUMER_ARCHIVE_POSTS = Table("forumer_archive_posts", (
    Column("topic_id", INT, nullable=False),
    Column("position", INT, nullable=False),  # 0-based, in the topic
    Column("author", TEXT),
    Column("posted_on", DATETIME),            # day only
    Column("html", LONGTEXT),
    Column("post_id", INT),                   # -> posts.id
), primary_key=("topic_id", "position"))

FORUMER_EMOTICONS = Table("forumer_emoticons", (
    Column("url", TEXT, nullable=False),
    Column("code", TEXT),                     # what members typed, e.g. ":lol:"
), primary_key=("url",))

# Members' avatars on the old board (see recover.recover_avatars).
FORUMER_AVATARS = Table("forumer_avatars", (
    Column("old_id", INT, nullable=False),    # -> forumer_members.old_id
    Column("url", TEXT),                      # the avatar's original URL
    Column("content_type", TEXT),
    Column("data", BLOB),
    Column("recovered_from", TEXT),           # the Wayback capture
), primary_key=("old_id",))

TABLES = (FORUMER_MEMBERS, FORUMER_FORUMS, FORUMER_TOPICS, FORUMER_POLLS, FORUMER_POSTS, FORUMER_ATTACHMENTS, FORUMER_ARCHIVE_POSTS,
          FORUMER_EMOTICONS, FORUMER_AVATARS)
register(*TABLES)
