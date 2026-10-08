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

# Optional: log in (by hand, in a Chrome window), then add --login to dry-run/crawl/enrich.
tapascrape login metanetfr

# Original BBCode of every post into posts.source (needs `login`; ~9 h for 460k posts).
tapascrape sources metanetfr --db sqlite:///metanet.db
# Rebuild the BBCode of the few posts `sources` couldn't fetch, from the website's HTML.
tapascrape recover-sources metanetfr --db sqlite:///metanet.db --login

# Report markup problems in the sources (writes nothing); --rule NAME lists matching post ids.
tapascrape scan --db sqlite:///metanet.db
# Repair migration leftovers into posts.source_fixed (--show ID previews a post; writes nothing).
tapascrape smilies --db sqlite:///metanet.db   # smiley images -> smilies table (Wayback / Tapatalk)
tapascrape fix --db sqlite:///metanet.db       # also lists attachments in the attachments table
tapascrape attachments --db sqlite:///metanet.db   # recover attachment files (Wayback)
tapascrape scan --db sqlite:///metanet.db --fixed   # what's left
tapascrape quotes --db sqlite:///metanet.db   # the posts quotes quote -> quotes table
tapascrape site --db sqlite:///metanet.db --out site --title "Metanet Forums"   # static HTML site

# HTML pass: rank, signature, group names; recovers posts the API can't return.
tapascrape enrich metanetfr --db sqlite:///metanet.db

# Re-download avatars stored before downloads bypassed Cloudflare's recompressed copies.
tapascrape avatars --db sqlite:///metanet.db

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
| posts | id, topic_id, user_id, index, timestamp, content, source, source_fixed |
| users | id, name, rank, joined_at, last_active_at, post_count, signature, signature_source, signature_fixed, avatar_url, avatar_id |
| avatars | id, user_id, data |
| groups | id, name |
| group_users | group_id, user_id |
| smilies | id, url, name, host, uses, content_type, data, recovered_from |
| attachments | id, kind, url, name, old_forum_id, uploaded_at, old_attach_id, first_post_id, uses, content_type, size, data, recovered_from |
| quotes | post_id, position, level, parent_position, author, date, quoted_post_id, quoted_user_id, match, similarity, tz_offset |
| crawl_state | key, value |

`finalize` computes these columns:

- For topics whose posts were scraped: `post_count`, `last_post_id` (the latest post by time) and `created_at` (the first post's time).
- For forums, over their own topics (subforums not included): `post_count`, `view_count` and `last_post_id`.

A few posts make Tapatalk's own backend fail with a MySQL collation error, for example an author name containing an emoji-range character. The API then errors for any range that includes them. The crawler narrows the range down to the bad posts, skips them, and records them as `post-gap:<topic>:<offset>` rows in `crawl_state`. `status` lists them. `enrich` recovers them from the topic's web page. Their content is then the website's HTML rendering, not the API's, and their timestamps have minute precision. Such posts are tagged `post-source:<id> = html` in `crawl_state`.

Images Tapatalk serves (avatars, files under the board's `forum_data/`) pass through Cloudflare, whose cache returns "polished" copies: recompressed, sometimes lossily, so animated GIFs can lose frames. They come back as WebP if the request accepts it. Downloads add a unique query parameter to bypass that cache, and refuse any answer marked `cf-polished`, so files are stored as they were uploaded.

Users come from the API's member list, which covers every registered member (including those who never posted) and doesn't need a login, unlike the website's. Authors missing from it (deleted accounts) keep a row with just `id` and `name`, taken from their posts.

## Post content vs. source

`posts.content` is what `get_thread` returns: HTML with only `<b>`, `<i>`, `<u>` and `<br />`, plus `[url]`, `[img]`, `[quote]` and `[spoiler]` BBCode. Everything else (`[color]`, `[size]`, `[list]`, `[font]`, ...) is stripped by the API by design.

`posts.source` is the post's original BBCode, unstripped. It's obtained through the API's Quote feature (`get_quote_post`), which only works when logged in.

- Posts are quoted in batches and the result is split back into posts. A batch that can't be split cleanly, or that the server fails on, is halved until the culprits are isolated.
- Posts with no source available are recorded as `source-gap:<id>` in `crawl_state`.
- Reruns only fetch posts whose `source` is still NULL.

`recover-sources` fills in those gaps from the topic's web page by turning the rendered HTML back into BBCode. Posts that already have a source are never touched. The board renders malformed tags as literal text, so they survive the round trip. Some details can't be recovered from the HTML: links lose the quotes around their URL, tags come back in lowercase, and spoiler titles are lost. On posts whose real source is known, about 80% come back identical. Rebuilt posts are tagged `source-origin:<id> = html` in `crawl_state`.

On boards migrated from older platforms (Yuku, InvisionFree), sources keep some import leftovers. Examples are `[table]` quote blocks, malformed tags like `[color=BLUE'>]`, and smileys hosted on dead sites. `scan` counts them rule by rule, with example posts. It also lists tags that don't open and close evenly, and bracketed words that aren't tags (`[sarcasm]`, ...).

`fix` writes a repaired copy of each source to `posts.source_fixed`. `posts.source` is never modified. Every run recomputes the whole column, so fixes can be changed and rerun at any time (`--only FIX` applies just some of them). The fixes:

| fix | before | after |
|---|---|---|
| attribute-leak | `[color=RED'>]` | `[color=RED]` |
| quoted-url | `[url='x']` | `[url=x]` |
| quoted-img | `[img]'x'[/img]` | `[img]x[/img]` |
| size-100 | `[size=100]x[/size]` | `x` |
| table-blocks | `[table][tr][td][b]QUOTE[/b] (name @ Nov 4 2005, 07:37 PM)[/td][/tr][tr][td]x[/td][/tr][/table]` | `[quote="name" date="Nov 4 2005, 07:37 PM"]x[/quote]` |
| | the same table with `CODE` | `[code]x[/code]` |
| smilies | `[img]http://static.yuku.com/.../tongue.gif[/img]` | `[ts:smiley=3]` |
| attachments | `--------------------[url=/attach/ma/post-10-1081445810.txt]Click here to view the attachment[/url]` | `[ts:attachment=7]` |
| | `[img]http://2.forumer.com/uploads/metanet/post-1-1181593950.png[/img]` | `[ts:attachment-image=8]` |
| | `[url=...index.php?act=Attach&type=post&id=52852]my level[/url]` | `[ts:attachment-link=9]my level[/ts:attachment-link]` |
| old-links | `[url=http://metanet.2.forumer.com/index.php?showtopic=996&st=20]here[/url]` | `[ts:topic=996 start=20]here[/ts:topic]` |

Tables and sizes are rewritten innermost first, so nested quotes come out right. Pairs that don't have the expected shape (unclosed, extra cells) are left as they are. The `date` attribute isn't standard phpBB. It keeps the date exactly as Yuku displayed it, in an unknown timezone.

The last two fixes write sentinels: tags in a reserved `ts:` namespace that the board never accepted (tag names can't contain `:`). They stand for things a later stage resolves, such as a static mirror turning them into local images and links:

- `[ts:smiley=ID]` is a row of the `smilies` table. `tapascrape smilies` gives every distinct smiley URL in posts and signatures a stable id: the dead hosts (Yuku, forumer, other Invision boards) and the board's own Tapatalk-hosted ones (`forum_data/.../smilies/`). It then downloads each image: Tapatalk-hosted ones from the board's website (asking for the original file, not Cloudflare's WebP conversion, and with the saved login if there is one), everything else from the Wayback Machine when it was archived. Run it before `fix`. Rerunning it only adds new URLs and looks up the images still missing. `--retry` also looks again for the ones found nowhere.
- `[ts:topic=N start=S old_post=P]text[/ts:topic]` and `[ts:forum=N]text[/ts:forum]` replace links to the old forumer board (`metanet.2.forumer.com`): named links, `[url]` autolinks and bare URLs. Topic and forum ids survived the migration, so `N` is the current id. Post ids didn't, so `old_post` (from `findpost`/`#entry` links) only records the old one. Only ids present in the database are converted. User profiles, searches, attachments and truncated URLs are left as they are.

- `[ts:attachment=ID]` (the block migrated posts end with), `[ts:attachment-image=ID]` (an embedded upload) and `[ts:attachment-link=ID]text[/ts:attachment-link]` (a link to one) are rows of the `attachments` table: files uploaded to the old forumer board. `fix` lists them before rewriting. The relative `/attach/ma/<file>` links were forumer's `http://2.forumer.com/uploads/metanet/<file>`, and the file name, `post-<forum>-<unix time>.<ext>`, gives the forum it was uploaded in (at the time) and the upload time. `act=Attach&id=N` downloads are listed by id. `tapascrape attachments` then recovers the files: forumer's domains are parked (any URL answers with the same HTML page, which is detected and not stored), so they come from the Wayback Machine. It lists each upload directory's captures with a single query, and only downloads files that were archived. Each download is checked to really be the file (images must be images, no HTML pages), and the original file name is kept when the archived response has one. Files found nowhere are recorded as `attachment-missing:<id>` and skipped on later runs unless `--retry` is given.

Sentinels are never written inside `[code]` blocks, nor in a post whose source already contains `[ts:`.

`fix` also processes signatures. Only their website HTML is available, so `users.signature_source` is BBCode rebuilt from it (the same way as `recover-sources`, with images Tapatalk serves through its `imageproxy.php` given their original URL back). `users.signature_fixed` is that BBCode with the same fixes applied.

## Quotes

`quotes` finds the post each `[quote]` quotes, so links can point at it. Run it after `fix`, since it reads `source_fixed`, falling back to `source` and then to the HTML rebuilt as BBCode. Each run rebuilds the whole `quotes` table.

Every quote gets a row, nested ones included. `position` is the 0-based order of its opening tag in the post's text, outside `[code]` blocks. `level` is 1 for a quote written in the post itself and 2 for a quote inside it. `parent_position` gives the enclosing quote, so the tree can be rebuilt. `author` and `date` are as the tag wrote them. The tag can be `[quote="name" date="..."]`, `[quote=name @ date]`, `[quote=name,date]` or `(name @ date)`, including the Invision variants that lost the month (`name @  25, 2007 03:11 pm`). Explicit ids are used directly when the tag has them: Tapatalk's `uid=`, XenForo's `post: N, member: N`, or `post=`/`timestamp=`.

A quote is matched against earlier posts on three kinds of evidence:

- **Author.** The name is the post author's, exactly or nearly (a typo or a prefix).
- **Date.** Boards showed dates in the reader's timezone, so the quote's date is the post's UTC time shifted by a real timezone offset (whole or half hours, or the :45 zones, between −12 h and +14 h) and cut to the minute. Each quoter's usual offset is learned from the clear-cut matches and breaks ties. `tz_offset` keeps the offset found.
- **Text.** The share of the quote's word trigrams found in the post's own text, leaving out its own quotes (`similarity`, in %). Along with the author or the date, it finds quoted posts in any topic.

`match` says what agreed:

| match | evidence |
|---|---|
| post-id | the tag's own post id |
| author+date | the author's post at that time |
| date+text | the date, plus the text (renamed or unknown author) |
| author+text | the author, plus the text (undated quote) |
| near-author+date | an almost-matching name at that time |
| text | at least 80% of the text alone, in the same topic (elsewhere, the same text is as likely quoted from where both took it) |
| date | the topic's only post at that time, for a quote naming no known member and too short to compare |
| author | the author's latest earlier post in the topic, for an undated quote too short to compare |
| ambiguous | several equally good candidates; `quoted_post_id` stays NULL |

Unmatched quotes keep `match` NULL. `quoted_user_id` is still set when the name belongs to a single member.

Author and date alone aren't enough when the quote is long enough to compare and its text isn't in the post. The exception is a post in the same topic at the quoter's usual offset, since posts get edited. The quoted post must be older than the quoting post. For a nested quote, it must be older than the post the enclosing quote matched, and that post's author is whose timezone applies.

## Static site

`site` writes a static HTML site to browse the board. It's plain HTML and CSS, with no JavaScript, so it can be opened from disk or served by any web server. Every style is in `style.css`, which can be edited by hand.

| page | what |
|---|---|
| `index.html` | the forum tree |
| `f/<id>.html` | a forum: its subforum tree, then its topics |
| `t/<id>.html` | a topic: every post, oldest first, each anchored as `#p<post id>` |
| `users.html` | every user, by id: name, rank, join and last-active times, post count, first and last post |
| `u/<id>.html` | a user: avatar, id, name, rank, groups, join and last-active times, post count, first and last post, signature (empty fields are left out) |
| `u/<id>-posts.html` | all of a user's posts, oldest first, shown as on topic pages (linked from their post count) |
| `files/` | the smileys, attachments and avatars stored in the database |

There's one page per forum, topic and user, with no pagination. Every page starts with the board's name (`--title`), a line of shortcuts to the board-wide pages (for now, the users list), and the breadcrumbs down to it.

- **Forum tables.** A forum's subforums are shown as a tree, nested levels indented. Each level is sorted by latest post: the forum's own, or a subforum's, whichever is newer. Each row shows the forum's own topic, post and view counts and its own last post.
- **Topic tables.** Topics are sorted by last post, with stickies first. The flags column shows `S` for a sticky and `L` for a locked topic.
- **Times.** All times are UTC, in ISO 8601.

Everything is plain text except post bodies and signatures, which are rendered from their BBCode (`source_fixed`, else `source`, else the API's HTML turned back into BBCode).

- **Markup.** Only tags the board renders become HTML. Malformed markup degrades as it did on the board: unknown, unclosed or unmatched tags stay as text. Bare URLs become links. Only `http`, `https`, `ftp` and `mailto` links are kept. Colors, sizes and fonts are the only inline styles.
- **Sentinels.** `[ts:smiley]` and `[ts:attachment...]` point at the files in `files/`. Ones never recovered show as lost. `[ts:topic]` and `[ts:forum]` become links to their pages.
- **Quotes.** A quote's heading links to the post it quotes, when `quotes` found it.

Rerunning `site` overwrites the pages but doesn't delete anything, so it's safe to rebuild into the same folder.

## Metanet: the Forumer era

Board-specific extras live under `tapascrape/boards/` and the `metanet` command group. The general commands never load them.

Before Yuku and Tapatalk, Metanet Forums was an Invision Power Board on Forumer (`metanet.2.forumer.com`). A 2019 Wayback Machine dump of that site holds what the migrations dropped. It covers about 36.7k posts with their old ids from 2004–2008, and the full member list with old ids.

```sh
tapascrape metanet import-dump forumer_wayback_machine --db sqlite:///metanet.db   # -> forumer_* tables
tapascrape metanet link --db sqlite:///metanet.db   # old ids -> ours; stores the dump's attachment files
tapascrape metanet attachments --db sqlite:///metanet.db   # archived act=Attach downloads (Wayback)
tapascrape metanet avatars --db sqlite:///metanet.db       # forumer-era avatars -> forumer_avatars (Wayback)
tapascrape metanet quotes --db sqlite:///metanet.db        # `quotes`, also knowing forumer-era names
tapascrape metanet site --db sqlite:///metanet.db --out site   # `site`, with what only the dump has
```

Both commands can be rerun. `import-dump` skips the dump's error, login-only and parked-domain pages. It understands the board's skins, which use different date formats.

| table | what |
|---|---|
| forumer_members | old_id, name, user_id, match, group_name, title, joined_at, post_count, avatar_url, country, signature, birthday, location, specific_location, interests, website, msn, aim, yahoo, icq, integrity |
| forumer_posts | id (old post id), topic_id, forum_id, post_id, match, member_id, author, posted, posted_at, html, edited_by, edited_at, source_file |
| forumer_forums | id, name, description, parent_id, category, in_tapatalk: forums named by the pages' navigation and forum listings |
| forumer_topics | id, forum_id, title, description, started_at, pinned, poll (JSON with vote counts), in_tapatalk |
| forumer_attachments | old_post_id, ref, kind, name (original file name), downloads, attachment_id, content_type, data, source_file |
| forumer_archive_posts | topic_id, position, author, posted_on, html, post_id: the lite archive (`a/`), which has no post ids |
| forumer_emoticons | url, code: what members typed for each smiley image |
| forumer_avatars | old_id, url, content_type, data, recovered_from: each member's last avatar on the old board |

The profile fields (birthday, location, messenger ids, which are often e-mail addresses) are personal data. Keep them out of anything published.

`link` fills the columns that point at our tables, and `match` says how each link was made:

- **Members.** Tapatalk numbered the migrated members in their old order.
  - Unique exact names anchor the mapping (`name`).
  - Members between two anchors pair up by position when both sides have the same number (`position`). This gives back the names of the ~800 accounts Tapatalk lists only by their id. Join dates agree on every checkable pair.
  - Members left over take the author of their linked posts (`posts`).
- **Posts.** Topic ids survived the migrations, and forumer showed UTC times to the minute.
  - A post is ours if it is from the same topic and the same minute (`time`).
  - Ties are broken by author (`time+author`), then by order (`time+order`).
  - Guest posts (Tapatalk's `user_id` 0) get their author's name this way.
- **Linked old post ids** (`old_post=` in `[ts:topic]` sentinels) that the dump lacks are placed between their matched neighbours, since old ids grow with time. The link is made when the topic has a single post in that window (`interpolated`). Holding out 3,000 known posts, this placed 99% and placed none wrongly.
- **Attachments.** Forumer's `act=Attach&id=N` used the post's own id. So a linked post ties its attachment box (original name, download count) to the `attachments` row of the file its source links. Files the dump has are stored there (`recovered_from = forumer-dump:<file>`).

Topics the dump has but Tapatalk doesn't keep their posts in `forumer_posts` (`post_id` NULL, `forumer_topics.in_tapatalk` false).

`link` also ties each `act=Attach&id=N` row of `attachments` to the upload row of post N, which is the same file. When only one of the two has the file, it copies it to the other.

`metanet attachments` lists every archived `act=Attach` download, including the session-prefixed URLs (`index.php?s=…&act=Attach…`) that the generic `attachments` command can't see. For each one it fills:
- the `act=Attach` row of that id;
- the upload row of the post it belongs to. That is the linked post when there is one. Otherwise it is the only unlinked post in the id's time window whose upload is still missing, and only when the archived file name has the same extension (guesses that don't check out are dropped).

Outcomes are recorded as `forumer-attach:<id>` in `crawl_state`. `--retry` looks again.

`metanet avatars` fills `forumer_avatars` with each member's last avatar on the old board:
- Uploaded ones (`uploads/metanet/av-<old id>.<ext>`) come from a single listing of the archived uploads. The file the dump last saw is preferred, then the latest capture.
- Avatars linked from image hosts are looked up one by one.

Images are checked to be images. Outcomes are recorded as `forumer-avatar:<old id>`.

`metanet quotes` runs the general `quotes` matching with more names: each member's forumer name, and the author forumer showed on each linked post, guests included. Quotes name people as they were called at the time, and some accounts are known to Tapatalk only by their id, so use it instead of `quotes` on this board.

`metanet site` builds the general static site, plus what only the dump has:

- **Forums and topics Tapatalk never got.** That includes forum 40, "N Webcomics", with its topics and the posts the dump has of them. Forumer's rendered HTML is turned back into BBCode and run through `fix`, like the migrated posts, so quote tables, smileys, uploads and old links come out the same. Their posts are anchored by their old id (`#o<old id>`). Topics only known from forum listings get a page saying none of their posts were archived.
- **Topic descriptions,** in a column of the topic tables.
- **Members.** Accounts Tapatalk only knows by their id get their forumer name. Members Tapatalk lacks get a page of their own (`u/f<old id>.html`). Profiles add the forumer id, the forumer member title ("Old title", when it isn't the rank) and main group, and, when known, location, website, birthday, messenger ids and interests. The users list starts with an "Old ID" column. A member's forumer-era avatar is shown under the Tapatalk one when the two differ.
- **Guest posts** show the name forumer showed for them.
- **Links to old post ids** (`[ts:topic … old_post=N]`) point at that post. Smileys are titled with the code members typed for them.
- **Links to NUMA** (`numa.notdot.net`, now dead) go to its new home, `https://www.nmaps.net`, with the same path, keeping their text. Map pages lose their `/map` (`/map/85674` → `/85674`), and author searches become queries (`browse?sort=created&author=X` → `browse?sort=created&q=author:X`, other parameters kept). Images (`[img]`) are left alone.

Polls aren't shown yet. The profile fields are personal data, as noted above, so mind them before publishing the site.

## Login

A login is only needed for content hidden from guests, such as private forums. `tapascrape login <board>` opens the board's login page in Chrome:

- Log in there with any method: Tapatalk ID, Google, or a legacy forum account. The tool never sees your password.
- The Chrome profile and a `session.json` (cookies + User-Agent) are saved in `~/.tapascrape/<board>/`, or wherever `--session-dir` points. They hold live login cookies, so keep them private.
- `--login` makes `dry-run`, `crawl` and `enrich` use the saved session, for both API calls and web pages.
- If the session has expired, the API falls back to guest access with a warning. Run `login` again.

## Cloudflare

The website (unlike the API) is behind a Cloudflare challenge. `enrich` fetches pages with curl_cffi. When it's challenged, it opens a visible Chrome window through zendriver:

- If Cloudflare challenges the window, it usually passes on its own. If a checkbox appears, click it.
- The window's cookies are then copied to curl_cffi.
- If curl_cffi is still challenged, the remaining pages are fetched through that Chrome window. This is the usual case.

Headless Chrome does not pass the challenge.

To avoid zendriver, export cookies from a browser that can open the board, in Netscape `cookies.txt` format. Pass them with `--cookies cookies.txt --user-agent "<that browser's UA>" --no-browser`. Use `--impersonate firefox` or `--impersonate safari` if the cookies don't come from Chrome.
