"""SQLite adapter."""

import sqlite3
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from tapascrape.db import schema
from tapascrape.db.base import Database
from tapascrape.db.schema import Column, Table

_TYPES = {
    schema.INT: "INTEGER",
    schema.BOOL: "INTEGER",
    schema.TEXT: "TEXT",
    schema.LONGTEXT: "TEXT",
    schema.DATETIME: "TEXT",  # ISO 8601 "YYYY-MM-DD HH:MM:SS", UTC
    schema.BLOB: "BLOB",
}


def _adapt(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, bool):
        return int(value)
    return value


class SQLiteDatabase(Database):
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")

    def quote_ident(self, name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    def column_ddl(self, table: Table, column: Column) -> str:
        ddl = f"{self.quote_ident(column.name)} {_TYPES[column.type]}"
        if column.autoincrement:
            return ddl + " PRIMARY KEY AUTOINCREMENT"
        if not column.nullable:
            ddl += " NOT NULL"
        return ddl

    def upsert_sql(self, table: Table, columns: Sequence[str]) -> str:
        cols = ", ".join(self.quote_ident(c) for c in columns)
        marks = ", ".join("?" for _ in columns)
        keys = ", ".join(self.quote_ident(c) for c in table.primary_key)
        updates = [c for c in columns if c not in table.primary_key]
        sql = f"INSERT INTO {self.quote_ident(table.name)} ({cols}) VALUES ({marks}) ON CONFLICT ({keys}) "
        if not updates:
            return sql + "DO NOTHING"
        sets = ", ".join(f"{self.quote_ident(c)} = excluded.{self.quote_ident(c)}" for c in updates)
        return sql + f"DO UPDATE SET {sets}"

    def existing_columns(self, table: Table) -> set[str] | None:
        rows = self.conn.execute(f"PRAGMA table_info({self.quote_ident(table.name)})").fetchall()
        return {r[1] for r in rows} or None

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        return self.conn.execute(sql, [_adapt(p) for p in params]).lastrowid

    def _executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        self.conn.executemany(sql, [[_adapt(v) for v in row] for row in rows])

    def _fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[tuple]:
        return self.conn.execute(sql, [_adapt(p) for p in params]).fetchall()

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    def close(self) -> None:
        self.conn.close()
