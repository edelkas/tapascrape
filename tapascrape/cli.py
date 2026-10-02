"""Command-line entry point."""

import argparse
import logging
import sys

from tapascrape.config import BoardConfig
from tapascrape.crawl.enrich import enrich_profiles, pending_profiles, recover_gaps
from tapascrape.crawl.finalize import finalize
from tapascrape.crawl.forums import print_tree, store_forums, survey
from tapascrape.crawl.posts import GAP_PREFIX, crawl_posts, pending_topics
from tapascrape.crawl.sources import GAP_PREFIX as SOURCE_GAP_PREFIX
from tapascrape.crawl.sources import crawl_sources, pending_sources
from tapascrape.crawl.topics import crawl_topics
from tapascrape.crawl.users import crawl_members, crawl_users, pending_users
from tapascrape.db import open_database
from tapascrape.db.schema import TABLES
from tapascrape.net.api import AuthError, TapatalkApi
from tapascrape.net.session import Session, session_dir
from tapascrape.net.web import WebClient, browser_login

log = logging.getLogger("tapascrape")


def load_session(args: argparse.Namespace) -> Session | None:
    if not args.login:
        return None
    session = Session.load(session_dir(args.board, args.session_dir))
    log.info("using the saved session of %s", session.username or f"user {session.user_id}")
    return session


def make_api(args: argparse.Namespace, session: Session | None = None) -> TapatalkApi:
    api = TapatalkApi(BoardConfig(args.board, rate=args.rate), session=session)
    if session is not None and not api.logged_in():
        log.warning("the API doesn't accept the saved session (expired?); continuing as a guest. "
                    "Run `tapascrape login %s` to log in again", args.board)
    return api


def interactive_login(args: argparse.Namespace) -> tuple[Session, bool]:
    """Log in through the Chrome window and save the session; (session, API accepts it)."""
    config = BoardConfig(args.board, rate=args.rate)
    directory = session_dir(args.board, args.session_dir)
    session = browser_login(config, directory / "chrome-profile")
    api = TapatalkApi(config, session=session)
    accepted = api.logged_in()
    if accepted:
        session.username = api.get_user(session.user_id).name
    session.save(directory)
    return session, accepted


def cmd_login(args: argparse.Namespace) -> int:
    session, accepted = interactive_login(args)
    if accepted:
        print(f"Logged in as {session.username} (user {session.user_id}); the API accepts the session.")
    else:
        print(f"Logged in on the website as user {session.user_id}, but the API doesn't accept "
              "the session: API calls will stay anonymous.")
    print(f"Session saved in {session_dir(args.board, args.session_dir)} "
          "(keep it private; it holds your login cookies).")
    return 0


def cmd_dry_run(args: argparse.Namespace) -> int:
    api = make_api(args, load_session(args))
    roots = api.forum_tree()
    counts = survey(api, roots, count_posts=not args.no_posts)
    total = print_tree(roots, counts, sys.stdout)

    stats = api.board_stats()
    posts = "?" if total.posts is None else f"{total.posts:,}"
    print()
    print(f"Visible total: {total.topics:,} topics, {posts} posts")
    print(f"Board stats:   {int(stats.get('total_threads', 0)):,} topics, "
          f"{int(stats.get('total_posts', 0)):,} posts, "
          f"{int(stats.get('total_members', 0)):,} members")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    api = make_api(args)
    with open_database(args.db) as db:
        db.create_schema()
        n = store_forums(db, api.forum_tree())
    print(f"Stored {n} forums in {args.db}")
    return 0


def cmd_crawl(args: argparse.Namespace) -> int:
    api = make_api(args, load_session(args))
    with open_database(args.db) as db:
        db.create_schema()
        roots = api.forum_tree()
        store_forums(db, roots)
        forums = [f for root in roots for f, _ in root.walk()]
        forum_ids = None
        if args.forum:
            unknown = set(args.forum) - {f.id for f in forums}
            if unknown:
                log.error("unknown forum ids: %s", sorted(unknown))
                return 2
            forums = [f for f in forums if f.id in args.forum]
            forum_ids = [f.id for f in forums]

        log.info("topics: %d listed", crawl_topics(api, db, forums, refresh=args.refresh_topics))
        if not args.no_posts:
            crawl_posts(api, db, forum_ids)
        if not args.no_users:
            avatars = not args.no_avatars
            if not args.no_members:
                crawl_members(api, db, avatars=avatars, refresh=args.refresh_users)
            # Authors missing from the member list (typically deleted accounts).
            crawl_users(api, db, avatars=avatars, refresh=args.refresh_users)
        finalize(db)
        print_status(db, sys.stdout)
    return 0


def cmd_enrich(args: argparse.Namespace) -> int:
    config = BoardConfig(args.board, rate=args.rate)
    session = load_session(args)
    profile_dir = session_dir(args.board, args.session_dir) / "chrome-profile" if session else None
    with open_database(args.db) as db, WebClient(
            config, cookies_file=args.cookies, user_agent=args.user_agent,
            use_browser=not args.no_browser, impersonate=args.impersonate,
            login=session, profile_dir=profile_dir) as web:
        db.create_schema()
        if not args.no_gaps:
            recover_gaps(web, db)
        if not args.no_profiles:
            enrich_profiles(web, db, refresh=args.refresh)
        finalize(db)
        print_status(db, sys.stdout)
    return 0


MAX_RELOGINS = 3


def cmd_sources(args: argparse.Namespace) -> int:
    # The Quote feature is members-only: this always needs a session, and logs in
    # again (Chrome window) when there's none or it has expired.
    config = BoardConfig(args.board, rate=args.rate)
    try:
        session = Session.load(session_dir(args.board, args.session_dir))
    except FileNotFoundError:
        session = None
    with open_database(args.db) as db:
        db.create_schema()
        logins = 0
        while True:
            if session is None or not TapatalkApi(config, session=session).logged_in():
                if logins == MAX_RELOGINS:
                    log.error("still not logged in after %d login attempts; giving up", logins)
                    return 2
                log.warning("not logged in (no session, or it expired): opening Chrome to log in")
                session, accepted = interactive_login(args)
                logins += 1
                if not accepted:
                    log.error("the API doesn't accept the website session; can't fetch sources")
                    return 2
            try:
                crawl_sources(TapatalkApi(config, session=session), db, args.forum,
                              batch_size=args.batch_size, retry_gaps=args.retry_gaps)
                break
            except AuthError:
                log.warning("the session expired during the run")
                session = None
        print_status(db, sys.stdout)
    return 0


def cmd_finalize(args: argparse.Namespace) -> int:
    with open_database(args.db) as db:
        db.create_schema()
        finalize(db)
        print_status(db, sys.stdout)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    with open_database(args.db) as db:
        db.create_schema()
        print_status(db, sys.stdout)
    return 0


def print_status(db, out) -> None:
    for table in TABLES:
        (count,), = db.query(f"SELECT COUNT(*) FROM {db.quote_ident(table.name)}")
        print(f"{table.name:>12}: {count:,}", file=out)
    print(f"{'pending':>12}: {len(pending_topics(db, None)):,} topics, "
          f"{len(pending_users(db)):,} users, {len(pending_profiles(db)):,} HTML profiles, "
          f"{len(pending_sources(db)):,} post sources", file=out)
    source_gaps = len(db.states(SOURCE_GAP_PREFIX))
    if source_gaps:
        print(f"{'no source':>12}: {source_gaps:,} posts", file=out)
    gaps = sorted(tuple(map(int, key.split(":"))) for key in db.states(GAP_PREFIX))
    if gaps:
        listed = ", ".join(f"topic {t} #{o + 1}" for t, o in gaps[:10])
        more = f" (+{len(gaps) - 10} more)" if len(gaps) > 10 else ""
        print(f"{'unfetchable':>12}: {len(gaps):,} posts: {listed}{more}", file=out)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tapascrape", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    def board_command(name: str, help: str, login: bool = True) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help)
        p.add_argument("board", help='board name, e.g. "metanetfr"')
        p.add_argument("--rate", type=float, default=1.0, help="max requests per second (default 1)")
        p.add_argument("--session-dir", metavar="DIR",
                       help="where `login` keeps the session (default ~/.tapascrape/<board>)")
        if login:
            p.add_argument("--login", action="store_true",
                           help="use the session saved by `tapascrape login`")
        return p

    p = board_command("login", "log in by hand in a Chrome window and save the session", login=False)
    p.set_defaults(func=cmd_login)

    p = board_command("dry-run", "print the forum tree with topic/post counts; writes nothing")
    p.add_argument("--no-posts", action="store_true",
                   help="only count topics (much faster; skips listing every topic)")
    p.set_defaults(func=cmd_dry_run)

    p = board_command("init", "create the schema and store the forum tree", login=False)
    p.add_argument("--db", required=True, help="e.g. sqlite:///metanet.db")
    p.set_defaults(func=cmd_init)

    p = board_command("crawl", "scrape forums, topics, posts and users (resumable)")
    p.add_argument("--db", required=True, help="sqlite:///file.db or mysql://user:pass@host/db")
    p.add_argument("--forum", type=int, action="append", metavar="ID",
                   help="only these forums (repeatable; subforums are not implied)")
    p.add_argument("--refresh-topics", action="store_true", help="re-list topics of done forums")
    p.add_argument("--refresh-users", action="store_true", help="re-fetch already stored users")
    p.add_argument("--no-posts", action="store_true", help="skip fetching posts")
    p.add_argument("--no-users", action="store_true", help="skip fetching users")
    p.add_argument("--no-members", action="store_true",
                   help="only fetch users who posted, not the whole member list")
    p.add_argument("--no-avatars", action="store_true", help="skip downloading avatars")
    p.set_defaults(func=cmd_crawl)

    p = board_command("enrich", "HTML pass: rank, signature, group names; recover unfetchable posts")
    p.add_argument("--db", required=True, help="sqlite:///file.db or mysql://user:pass@host/db")
    p.add_argument("--refresh", action="store_true", help="re-fetch already enriched profiles")
    p.add_argument("--no-profiles", action="store_true", help="skip user profiles")
    p.add_argument("--no-gaps", action="store_true", help="skip recovering unfetchable posts")
    p.add_argument("--cookies", metavar="FILE", help="cookies.txt (Netscape format) from your browser")
    p.add_argument("--user-agent", help="exact User-Agent of the browser the cookies come from")
    p.add_argument("--no-browser", action="store_true",
                   help="never open Chrome to pass Cloudflare (needs --cookies)")
    p.add_argument("--impersonate", default="chrome",
                   help="curl_cffi browser fingerprint matching the cookies' browser (default chrome)")
    p.set_defaults(func=cmd_enrich)

    p = board_command("sources", "fetch posts' original BBCode into posts.source (needs `login`)",
                      login=False)
    p.add_argument("--db", required=True, help="sqlite:///file.db or mysql://user:pass@host/db")
    p.add_argument("--forum", type=int, action="append", metavar="ID",
                   help="only posts in these forums (repeatable)")
    p.add_argument("--batch-size", type=int, default=100, help="posts per request (default 100)")
    p.add_argument("--retry-gaps", action="store_true",
                   help="try again the posts previously recorded as having no source")
    p.set_defaults(func=cmd_sources)

    for name, func, help in (("finalize", cmd_finalize, "recompute aggregate columns"),
                             ("status", cmd_status, "show row counts and pending work")):
        p = sub.add_parser(name, help=help)
        p.add_argument("--db", required=True, help="sqlite:///file.db or mysql://user:pass@host/db")
        p.set_defaults(func=func)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    sys.stdout.reconfigure(errors="replace")  # odd forum names vs. a legacy console codepage
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stderr)
    # -v is for our own debug output (every request made); libraries stay quiet,
    # and zendriver's chatty startup logs are hidden either way.
    logging.getLogger("tapascrape").setLevel(logging.DEBUG if args.verbose else logging.INFO)
    logging.getLogger("zendriver").setLevel(logging.WARNING)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except Exception as e:  # noqa: BLE001 - last resort: a readable message, not a trace
        log.debug("unhandled error", exc_info=True)
        log.error("%s: %s", type(e).__name__, e)
        log.error("Progress so far is saved; rerun the same command to resume "
                  "(add -v for the full traceback).")
        return 1
