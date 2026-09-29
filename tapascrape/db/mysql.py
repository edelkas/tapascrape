"""MySQL / MariaDB adapter (PyMySQL)."""

from collections.abc import Sequence
from typing import Any

import pymysql

from tapascrape.db import schema
from tapascrape.db.base import Database
from tapascrape.db.schema import Column, Table

_TYPES = {
    schema.INT: "BIGINT",
    schema.BOOL: "TINYINT(1)",
    schema.TEXT: "VARCHAR(512)",
    schema.LONGTEXT: "LONGTEXT",
    schema.DATETIME: "DATETIME",  # UTC
    schema.BLOB: "LONGBLOB",
}


def _placeholders(sql: str) -> str:
    # Callers use "?"; PyMySQL expects "%s". Our SQL has no literal "%" or "?".
    return sql.replace("?", "%s")


class MySQLDatabase(Database):
    def __init__(self, host: str, user: str, password: str, database: str, port: int = 3306):
        self.database = database
        self.conn = pymysql.connect(
            host=host, port=port, user=user, password=password, database=database,
            charset="utf8mb4", autocommit=False)

    def quote_ident(self, name: str) -> str:
        return "`" + name.replace("`", "``") + "`"

    def column_ddl(self, table: Table, column: Column) -> str:
        ddl = f"{self.quote_ident(column.name)} {_TYPES[column.type]}"
        if column.autoincrement:
            return ddl + " NOT NULL AUTO_INCREMENT PRIMARY KEY"
        if not column.nullable:
            ddl += " NOT NULL"
        return ddl

    def create_table_statements(self, table: Table) -> list[str]:
        # MySQL has no CREATE INDEX IF NOT EXISTS, so indexes go inline.
        sql = self.create_table_sql(table)
        extra = "".join(
            f", INDEX {self.quote_ident(ix.name)} ({', '.join(self.quote_ident(c) for c in ix.columns)})"
            for ix in table.indexes)
        sql = sql[:-1] + extra + ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
        return [sql]

    def existing_columns(self, table: Table) -> set[str] | None:
        rows = self._fetchall(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?", (self.database, table.name))
        return {r[0] for r in rows} or None

    def upsert_sql(self, table: Table, columns: Sequence[str]) -> str:
        cols = ", ".join(self.quote_ident(c) for c in columns)
        marks = ", ".join("?" for _ in columns)
        updates = [c for c in columns if c not in table.primary_key] or [table.primary_key[0]]
        sets = ", ".join(f"{self.quote_ident(c)} = VALUES({self.quote_ident(c)})" for c in updates)
        return (f"INSERT INTO {self.quote_ident(table.name)} ({cols}) VALUES ({marks}) "
                f"ON DUPLICATE KEY UPDATE {sets}")

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        with self.conn.cursor() as cur:
            cur.execute(_placeholders(sql), tuple(params))
            return cur.lastrowid

    def _executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        with self.conn.cursor() as cur:
            cur.executemany(_placeholders(sql), [tuple(r) for r in rows])

    def _fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[tuple]:
        with self.conn.cursor() as cur:
            cur.execute(_placeholders(sql), tuple(params))
            return list(cur.fetchall())

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    def close(self) -> None:
        self.conn.close()
