"""Abstract database interface. Adapters implement the backend specifics."""

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from typing import Any

from tapascrape.db.schema import CRAWL_STATE, TABLES, TABLES_BY_NAME, Table

Row = Mapping[str, Any]


class Database(ABC):
    """Minimal SQL surface the crawler needs.

    Parameters in `execute`/`query` use the `?` placeholder style; adapters
    translate it when their driver expects something else.
    """

    # -- backend specifics ----------------------------------------------

    @abstractmethod
    def quote_ident(self, name: str) -> str: ...

    @abstractmethod
    def column_ddl(self, table: Table, column) -> str: ...

    @abstractmethod
    def upsert_sql(self, table: Table, columns: Sequence[str]) -> str:
        """INSERT statement that updates the non-key `columns` on key conflict."""

    @abstractmethod
    def existing_columns(self, table: Table) -> set[str] | None:
        """Column names of `table` as it exists in the database, None if absent."""

    @abstractmethod
    def _execute(self, sql: str, params: Sequence[Any] = ()) -> Any: ...

    @abstractmethod
    def _executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None: ...

    @abstractmethod
    def _fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[tuple]: ...

    @abstractmethod
    def commit(self) -> None: ...

    @abstractmethod
    def rollback(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    # -- shared behaviour ------------------------------------------------

    def create_schema(self, tables: Sequence[Table] = TABLES) -> None:
        """Create missing tables and add (or rename) columns changed since the DB was made."""
        for table in tables:
            existing = self.existing_columns(table)
            if existing is None:
                for sql in self.create_table_statements(table):
                    self._execute(sql)
                continue
            for old, new in table.renamed:
                if old in existing and new not in existing:
                    self._execute(f"ALTER TABLE {self.quote_ident(table.name)} RENAME COLUMN "
                                  f"{self.quote_ident(old)} TO {self.quote_ident(new)}")
                    existing = (existing - {old}) | {new}
            for column in table.columns:
                if column.name not in existing:
                    # Added columns stay nullable: old rows have no value for them.
                    relaxed = replace(column, nullable=True)
                    self._execute(f"ALTER TABLE {self.quote_ident(table.name)} "
                                  f"ADD COLUMN {self.column_ddl(table, relaxed)}")
        self.commit()

    def create_table_statements(self, table: Table) -> list[str]:
        return [self.create_table_sql(table),
                *(self.create_index_sql(table, index) for index in table.indexes)]

    def create_table_sql(self, table: Table) -> str:
        parts = [self.column_ddl(table, c) for c in table.columns]
        if not any(c.autoincrement for c in table.columns):
            pk = ", ".join(self.quote_ident(c) for c in table.primary_key)
            parts.append(f"PRIMARY KEY ({pk})")
        return f"CREATE TABLE IF NOT EXISTS {self.quote_ident(table.name)} ({', '.join(parts)})"

    def create_index_sql(self, table: Table, index) -> str:
        cols = ", ".join(self.quote_ident(c) for c in index.columns)
        return (f"CREATE INDEX IF NOT EXISTS {self.quote_ident(index.name)} "
                f"ON {self.quote_ident(table.name)} ({cols})")

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        self._execute(sql, params)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[tuple]:
        return self._fetchall(sql, params)

    def upsert_many(self, table_name: str, rows: Iterable[Row]) -> int:
        """Insert or update rows keyed by the table's primary key.

        Only the columns present in the first row are written; on conflict,
        columns not given keep their stored values.
        """
        rows = list(rows)
        if not rows:
            return 0
        table = TABLES_BY_NAME[table_name]
        columns = [c for c in table.column_names if c in rows[0]]
        sql = self.upsert_sql(table, columns)
        self._executemany(sql, [tuple(row[c] for c in columns) for row in rows])
        return len(rows)

    def update_many(self, table_name: str, rows: Iterable[Row]) -> int:
        """Update the given columns of existing rows (matched by primary key)."""
        rows = list(rows)
        if not rows:
            return 0
        table = TABLES_BY_NAME[table_name]
        columns = [c for c in table.column_names if c in rows[0] and c not in table.primary_key]
        sets = ", ".join(f"{self.quote_ident(c)} = ?" for c in columns)
        where = " AND ".join(f"{self.quote_ident(c)} = ?" for c in table.primary_key)
        sql = f"UPDATE {self.quote_ident(table.name)} SET {sets} WHERE {where}"
        self._executemany(sql, [tuple(row[c] for c in (*columns, *table.primary_key)) for row in rows])
        return len(rows)

    def insert(self, table_name: str, row: Row) -> int:
        """Plain insert; returns the generated id (for auto-increment tables)."""
        table = TABLES_BY_NAME[table_name]
        columns = [c for c in table.column_names if c in row]
        cols = ", ".join(self.quote_ident(c) for c in columns)
        marks = ", ".join("?" for _ in columns)
        return self._execute(
            f"INSERT INTO {self.quote_ident(table.name)} ({cols}) VALUES ({marks})",
            [row[c] for c in columns])

    @contextmanager
    def transaction(self) -> Iterator["Database"]:
        try:
            yield self
        except BaseException:
            self.rollback()
            raise
        else:
            self.commit()

    # -- crawl state -----------------------------------------------------

    def get_state(self, key: str) -> str | None:
        rows = self.query(
            f"SELECT {self.quote_ident('value')} FROM {self.quote_ident(CRAWL_STATE.name)} "
            f"WHERE {self.quote_ident('key')} = ?", (key,))
        return rows[0][0] if rows else None

    def set_state(self, key: str, value: str) -> None:
        self.upsert_many(CRAWL_STATE.name, [{"key": key, "value": value}])

    def delete_state(self, key: str) -> None:
        self.execute(f"DELETE FROM {self.quote_ident(CRAWL_STATE.name)} "
                     f"WHERE {self.quote_ident('key')} = ?", (key,))

    def states(self, prefix: str) -> dict[str, str]:
        """All crawl-state entries whose key starts with `prefix`, keyed by the rest."""
        rows = self.query(
            f"SELECT {self.quote_ident('key')}, {self.quote_ident('value')} "
            f"FROM {self.quote_ident(CRAWL_STATE.name)} WHERE {self.quote_ident('key')} LIKE ?",
            (prefix + "%",))
        # LIKE treats "_" as a wildcard; the startswith check makes it exact.
        return {k[len(prefix):]: v for k, v in rows if k.startswith(prefix)}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
