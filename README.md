# tapascrape

Scrape Tapatalk boards (e.g. `metanetfr`) into SQL databases (SQLite or MySQL).

Data comes from Tapatalk's mobile XML-RPC API (`/groups/<board>/mobiquo/mobiquo.php`). The HTML site is behind a Cloudflare challenge, while the API gives exact view counts, sticky/locked flags, post positions and second-precision UTC timestamps.

```sh
pip install -e .[dev]

# Forum tree with topic and post counts (writes nothing).
tapascrape dry-run metanetfr            # lists every topic to count posts (~7 min at 1 req/s)
tapascrape dry-run metanetfr --no-posts # topic counts only (~1 min)

# Full scrape: forums -> topics -> posts -> users (+avatars) -> finalize.
tapascrape crawl metanetfr --db sqlite:///metanet.db
tapascrape crawl metanetfr --db mysql://user:pass@localhost/metanet   # database must exist
tapascrape crawl metanetfr --db sqlite:///test.db --forum 54          # a single forum

tapascrape status   --db sqlite:///metanet.db   # row counts and pending work
tapascrape finalize --db sqlite:///metanet.db   # recompute aggregate columns
```

`crawl` is resumable. Progress is stored in the `crawl_state` table, down to the page within a topic, so rerunning after Ctrl-C or an error continues where it stopped. Forums whose topics were already listed are skipped unless `--refresh-topics` is given. The same goes for fetched users and `--refresh-users`. Use `--rate` to set the maximum number of requests per second (default 1). The whole Metanet board takes about 16k requests.

## Schema

All IDs are the board's own IDs. Datetimes are UTC. `posts.content` and `users.signature` hold raw HTML.

| table | columns |
|---|---|
| forums | id, parent_id, name, description, last_post_id, post_count, view_count |
| topics | id, forum_id, user_id, name, stickied, locked, created_at, post_count, view_count, last_post_id |
| posts | id, topic_id, user_id, index, timestamp, content |
| users | id, name, rank, joined_at, last_active_at, post_count, signature, avatar_url, avatar_id |
| avatars | id, user_id, data |
| groups | id, name |
| group_users | group_id, user_id |
| crawl_state | key, value |

`finalize` computes these columns:

- For topics whose posts were scraped: `post_count`, `last_post_id` (the latest post by time) and `created_at` (the first post's time).
- For forums, over their own topics (subforums not included): `post_count`, `view_count` and `last_post_id`.

Users that only appear as authors but whose profiles are gone keep a row with just `id` and `name`. Rank, signature and group names need the HTML site and come in a later milestone.
