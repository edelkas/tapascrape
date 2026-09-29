"""Database adapters. Use `open_database(url)` to get one."""

from urllib.parse import unquote, urlparse

from tapascrape.db.base import Database


def open_database(url: str) -> Database:
    """Open a database from a URL.

    sqlite:///relative/path.db, sqlite:///C:/abs/path.db, or a bare file path.
    mysql://user:password@host:port/dbname (the database must already exist).
    """
    if "://" not in url:
        url = "sqlite:///" + url
    parsed = urlparse(url)
    if parsed.scheme == "sqlite":
        from tapascrape.db.sqlite import SQLiteDatabase

        path = unquote(url[len("sqlite:///"):]) or ":memory:"
        return SQLiteDatabase(path)
    if parsed.scheme == "mysql":
        from tapascrape.db.mysql import MySQLDatabase

        return MySQLDatabase(
            host=parsed.hostname or "localhost",
            port=parsed.port or 3306,
            user=unquote(parsed.username or "root"),
            password=unquote(parsed.password or ""),
            database=parsed.path.lstrip("/"),
        )
    raise ValueError(f"unsupported database URL scheme: {parsed.scheme!r}")
