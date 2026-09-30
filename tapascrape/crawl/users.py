"""Users (API) -> users, groups, group_users, avatars.

Two sources: the member list (every registered member) and get_user_info for
authors the list doesn't include (e.g. deleted accounts, which then fail).
"""

import logging

from tapascrape.crawl.progress import Progress
from tapascrape.db.base import Database
from tapascrape.models import User
from tapascrape.net.api import ApiError, TapatalkApi
from tapascrape.net.http import fetch_bytes
from tapascrape.net.throttle import RetryableError, Throttle

log = logging.getLogger(__name__)

STATE_PREFIX = "user:"  # value: "done" or "missing" (profile no longer available)


def pending_users(db: Database, refresh: bool = False) -> list[int]:
    """Users referenced anywhere (seeded rows, posts, topics) not fetched yet."""
    ids = {r[0] for r in db.query(
        "SELECT id FROM users UNION SELECT user_id FROM posts UNION SELECT user_id FROM topics")}
    ids.discard(None)
    if not refresh:
        ids -= {int(k) for k in db.states(STATE_PREFIX)}
    return sorted(i for i in ids if i > 0)


def crawl_users(api: TapatalkApi, db: Database, avatars: bool = True,
                avatar_throttle: Throttle | None = None, refresh: bool = False) -> int:
    todo = pending_users(db, refresh)
    if not todo:
        log.info("users: nothing to do")
        return 0
    progress = Progress("users", len(todo))
    avatar_throttle = avatar_throttle or Throttle(api.config.rate)
    stored = missing = 0
    for user_id in todo:
        try:
            user = api.get_user(user_id)
        except ApiError as e:
            # Deleted accounts: keep the row seeded from posts/topics, if any.
            log.debug("user %d: %s", user_id, e)
            db.set_state(f"{STATE_PREFIX}{user_id}", "missing")
            db.commit()
            missing += 1
            progress.advance()
            continue
        store_user(db, user, avatar_throttle if avatars else None)
        stored += 1
        progress.advance()
    log.info("users: %d stored, %d missing profiles", stored, missing)
    return stored


def crawl_members(api: TapatalkApi, db: Database, avatars: bool = True,
                  avatar_throttle: Throttle | None = None, refresh: bool = False,
                  per_page: int = 100) -> int:
    """Every member from the API's member list, including those who never posted.

    The list is cheap to page through (100 per call), so a rerun walks it again
    and only stores members not fetched yet.
    """
    avatar_throttle = avatar_throttle or Throttle(api.config.rate)
    done = set() if refresh else {int(k) for k, v in db.states(STATE_PREFIX).items() if v == "done"}
    seen: set[int] = set()
    stored = 0
    progress = None
    page = 1
    while True:
        count, users = api.member_page(page, per_page)
        if progress is None:
            progress = Progress("members", count)
        if not users:
            break
        for user in users:
            if user.id in seen:
                continue
            seen.add(user.id)
            if user.id not in done:
                store_user(db, user, avatar_throttle if avatars else None)
                stored += 1
        progress.advance(len(users))
        page += 1
    if progress and len(seen) < progress.total:
        # The list is ordered by activity, so members active during the walk can shift pages.
        log.warning("members: saw %d of %d; rerun to pick up the rest", len(seen), progress.total)
    log.info("members: %d listed, %d stored", len(seen), stored)
    return stored


def store_user(db: Database, user: User, avatar_throttle: Throttle | None) -> None:
    """Store an API user with groups; download the avatar if new (throttle given)."""
    previous = db.query("SELECT avatar_url, avatar_id FROM users WHERE id = ?", (user.id,))
    old_url, old_avatar = previous[0] if previous else (None, None)
    needs_avatar = avatar_throttle is not None and user.avatar_url and (
        old_avatar is None or old_url != user.avatar_url)
    with db.transaction():
        db.upsert_many("users", [{
            "id": user.id, "name": user.name, "joined_at": user.joined_at,
            "last_active_at": user.last_active_at, "post_count": user.post_count,
            "avatar_url": user.avatar_url}])
        db.upsert_many("groups", [{"id": g} for g in user.group_ids])
        db.execute("DELETE FROM group_users WHERE user_id = ?", (user.id,))
        db.upsert_many("group_users", [{"group_id": g, "user_id": user.id} for g in user.group_ids])
        if needs_avatar:
            store_avatar(db, user.id, user.avatar_url, avatar_throttle)
        db.set_state(f"{STATE_PREFIX}{user.id}", "done")


def store_avatar(db: Database, user_id: int, url: str, throttle: Throttle) -> None:
    """Download an avatar into `avatars` and point users.avatar_id at it."""
    try:
        data = fetch_bytes(url, throttle)
    except RetryableError as e:
        log.warning("avatar of user %d: %s", user_id, e)
        return
    if not data:
        return
    avatar_id = db.insert("avatars", {"user_id": user_id, "data": data})
    db.update_many("users", [{"id": user_id, "avatar_id": avatar_id}])
