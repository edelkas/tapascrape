# tapascrape

Scrape Tapatalk boards (e.g. `metanetfr`) into SQL databases (SQLite or MySQL).

Data comes from Tapatalk's mobile XML-RPC API (`/groups/<board>/mobiquo/mobiquo.php`). The HTML site is behind a Cloudflare challenge, while the API gives exact view counts, sticky/locked flags, post positions and second-precision UTC timestamps.

```sh
pip install -e .[dev,browser]

# Forum tree with topic and post counts (writes nothing).
tapascrape dry-run metanetfr            # lists every topic to count posts (~7 min at 1 req/s)
tapascrape dry-run metanetfr --no-posts # topic counts only (~1 min)

# Full scrape: forums -> topics -> posts -> users (+avatars) -> finalize.
tapascrape crawl metanetfr --db sqlite:///metanet.db
tapascrape crawl metanetfr --db mysql://user:pass@localhost/metanet   # database must exist
tapascrape crawl metanetfr --db sqlite:///test.db --forum 54          # a single forum

# HTML pass: rank, signature, group names; recovers posts the API can't return.
tapascrape enrich metanetfr --db sqlite:///metanet.db

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

A few posts make Tapatalk's own backend fail with a MySQL collation error, for example an author name containing an emoji-range character. The API then errors for any range that includes them. The crawler narrows the range down to the bad posts, skips them, and records them as `post-gap:<topic>:<offset>` rows in `crawl_state`. `status` lists them. `enrich` recovers them from the topic's web page. Their content is then the website's HTML rendering, not the API's, and their timestamps have minute precision. Such posts are tagged `post-source:<id> = html` in `crawl_state`.

Users that only appear as authors but whose profiles are gone keep a row with just `id` and `name`.

## Cloudflare

The website (unlike the API) is behind a Cloudflare challenge. `enrich` fetches pages with curl_cffi. When it's challenged, it opens a visible Chrome window through zendriver:

- If Cloudflare challenges the window, it usually passes on its own. If a checkbox appears, click it.
- The window's cookies are then copied to curl_cffi.
- If curl_cffi is still challenged, the remaining pages are fetched through that Chrome window. This is the usual case.

Headless Chrome does not pass the challenge.

To avoid zendriver, export cookies from a browser that can open the board, in Netscape `cookies.txt` format. Pass them with `--cookies cookies.txt --user-agent "<that browser's UA>" --no-browser`. Use `--impersonate firefox` or `--impersonate safari` if the cookies don't come from Chrome.
