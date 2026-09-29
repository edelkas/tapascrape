"""Command-line entry point."""

import argparse
import logging
import sys

from tapascrape.config import BoardConfig
from tapascrape.crawl.finalize import finalize
from tapascrape.crawl.forums import print_tree, store_forums, survey
from tapascrape.crawl.posts import crawl_posts, pending_topics
from tapascrape.crawl.topics import crawl_topics
from tapascrape.crawl.users import crawl_users, pending_users
from tapascrape.db import open_database
from tapascrape.db.schema import TABLES
from tapascrape.net.api import TapatalkApi

log = logging.getLogger("tapascrape")


def cmd_dry_run(args: argparse.Namespace) -> int:
    api = TapatalkApi(BoardConfig(args.board, rate=args.rate))
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
    api = TapatalkApi(BoardConfig(args.board, rate=args.rate))
    with open_database(args.db) as db:
        db.create_schema()
        n = store_forums(db, api.forum_tree())
    print(f"Stored {n} forums in {args.db}")
    return 0


def cmd_crawl(args: argparse.Namespace) -> int:
    api = TapatalkApi(BoardConfig(args.board, rate=args.rate))
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
            crawl_users(api, db, avatars=not args.no_avatars, refresh=args.refresh_users)
        finalize(db)
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
          f"{len(pending_users(db)):,} users", file=out)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tapascrape", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    def board_command(name: str, help: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help)
        p.add_argument("board", help='board name, e.g. "metanetfr"')
        p.add_argument("--rate", type=float, default=1.0, help="max requests per second (default 1)")
        return p

    p = board_command("dry-run", "print the forum tree with topic/post counts; writes nothing")
    p.add_argument("--no-posts", action="store_true",
                   help="only count topics (much faster; skips listing every topic)")
    p.set_defaults(func=cmd_dry_run)

    p = board_command("init", "create the schema and store the forum tree")
    p.add_argument("--db", required=True, help="e.g. sqlite:///metanet.db")
    p.set_defaults(func=cmd_init)

    p = board_command("crawl", "scrape forums, topics, posts and users (resumable)")
    p.add_argument("--db", required=True, help="sqlite:///file.db or mysql://user:pass@host/db")
    p.add_argument("--forum", type=int, action="append", metavar="ID",
                   help="only these forums (repeatable; subforums are not implied)")
    p.add_argument("--refresh-topics", action="store_true", help="re-list topics of done forums")
    p.add_argument("--refresh-users", action="store_true", help="re-fetch already stored users")
    p.add_argument("--no-posts", action="store_true", help="skip fetching posts")
    p.add_argument("--no-users", action="store_true", help="skip fetching user profiles")
    p.add_argument("--no-avatars", action="store_true", help="skip downloading avatars")
    p.set_defaults(func=cmd_crawl)

    for name, func, help in (("finalize", cmd_finalize, "recompute aggregate columns"),
                             ("status", cmd_status, "show row counts and pending work")):
        p = sub.add_parser(name, help=help)
        p.add_argument("--db", required=True, help="sqlite:///file.db or mysql://user:pass@host/db")
        p.set_defaults(func=func)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    sys.stdout.reconfigure(errors="replace")  # odd forum names vs. a legacy console codepage
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S", stream=sys.stderr)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
