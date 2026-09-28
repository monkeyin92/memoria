"""One SQL dialect, two backends for the Control API store.

The store's SQL is written once in the SQLite style (``?`` placeholders,
``BEGIN IMMEDIATE`` for single-writer sections). ``PostgresConnection``
adapts it to PostgreSQL so the same mixins run against either database:

* ``?`` becomes ``%s``; the SQL never contains a literal ``%`` or a quoted
  ``?``, which ``translate_sql`` checks.
* ``BEGIN IMMEDIATE`` becomes a transaction-scoped advisory lock, keeping the
  SQLite single-writer semantics instead of silently relaxing them to
  concurrent READ COMMITTED writers.
* Every write runs inside a savepoint, so a caught unique violation leaves the
  surrounding transaction usable, as it does in SQLite.
* Python booleans are sent as 0/1, matching the 0/1 BIGINT columns.
* Rows allow both ``row[0]`` and ``row["column"]`` and convert with ``dict()``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from typing import Any, Protocol

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg_pool import ConnectionPool

# Key of the transaction-scoped advisory lock that stands in for SQLite's
# database-wide write lock. Arbitrary but fixed; only this store takes it.
CONTROL_WRITE_LOCK_KEY = 0x4D454D4F  # "MEMO"

IntegrityError: tuple[type[Exception], ...] = (
    sqlite3.IntegrityError,
    psycopg.errors.IntegrityError,
)

_WRITE_PREFIXES = ("INSERT", "UPDATE", "DELETE")


class DbCursor(Protocol):
    @property
    def rowcount(self) -> int: ...

    def fetchone(self) -> Any: ...

    def fetchall(self) -> list[Any]: ...

    def __iter__(self) -> Iterator[Any]: ...


class DbConnection(Protocol):
    def execute(self, sql: str, parameters: Sequence[Any] = ..., /) -> DbCursor: ...

    def executemany(
        self, sql: str, parameters: Iterable[Sequence[Any]], /
    ) -> DbCursor: ...


def translate_sql(sql: str) -> str:
    """Rewrite SQLite ``?`` placeholders to psycopg ``%s``."""

    if "%" in sql:
        raise ValueError("control store SQL must not contain a literal %")
    parts = sql.split("'")
    # Even-indexed parts are outside single-quoted literals.
    for index in range(1, len(parts), 2):
        if "?" in parts[index]:
            raise ValueError("control store SQL must not quote a ?")
    return "'".join(
        part.replace("?", "%s") if index % 2 == 0 else part
        for index, part in enumerate(parts)
    )


def _adapt(parameters: Sequence[Any]) -> tuple[Any, ...]:
    return tuple(int(value) if isinstance(value, bool) else value for value in parameters)


class Row:
    """A result row readable by position, by column name and via ``dict()``."""

    __slots__ = ("_names", "_values")

    def __init__(self, names: tuple[str, ...], values: tuple[Any, ...]) -> None:
        self._names = names
        self._values = values

    def keys(self) -> list[str]:
        return list(self._names)

    def __getitem__(self, key: int | str) -> Any:
        if isinstance(key, int):
            return self._values[key]
        try:
            return self._values[self._names.index(key)]
        except ValueError:
            raise IndexError(f"no such column: {key}") from None

    def __iter__(self) -> Iterator[Any]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


class PostgresCursor:
    def __init__(self, cursor: psycopg.Cursor[tuple[Any, ...]]) -> None:
        self._cursor = cursor
        description = cursor.description
        self._names = tuple(column.name for column in description) if description else ()

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    @property
    def lastrowid(self) -> None:
        raise AttributeError("use INSERT ... RETURNING instead of lastrowid")

    def _row(self, values: tuple[Any, ...] | None) -> Row | None:
        return None if values is None else Row(self._names, tuple(values))

    def fetchone(self) -> Row | None:
        if not self._names:
            return None
        return self._row(self._cursor.fetchone())

    def fetchall(self) -> list[Row]:
        if not self._names:
            return []
        return [Row(self._names, tuple(values)) for values in self._cursor.fetchall()]

    def __iter__(self) -> Iterator[Row]:
        return iter(self.fetchall())


class PostgresConnection:
    """Present a psycopg connection with the subset of sqlite3's API we use."""

    def __init__(self, connection: psycopg.Connection[tuple[Any, ...]]) -> None:
        self._connection = connection

    def execute(self, sql: str, parameters: Sequence[Any] = (), /) -> PostgresCursor:
        statement = sql.strip()
        if statement.upper() == "BEGIN IMMEDIATE":
            cursor = self._connection.execute(
                "SELECT pg_advisory_xact_lock(%s)", (CONTROL_WRITE_LOCK_KEY,)
            )
            return PostgresCursor(cursor)
        translated = translate_sql(sql)
        values = _adapt(parameters)
        if statement.upper().startswith(_WRITE_PREFIXES):
            with self._connection.transaction():
                cursor = self._connection.execute(translated, values)
                return _BufferedCursor(cursor)
        return PostgresCursor(self._connection.execute(translated, values))

    def executemany(
        self, sql: str, parameters: Iterable[Sequence[Any]], /
    ) -> PostgresCursor:
        translated = translate_sql(sql)
        with self._connection.transaction():
            cursor = self._connection.cursor()
            cursor.executemany(translated, [_adapt(values) for values in parameters])
            return PostgresCursor(cursor)


class _BufferedCursor(PostgresCursor):
    """A write's result, fetched before its savepoint is released."""

    def __init__(self, cursor: psycopg.Cursor[tuple[Any, ...]]) -> None:
        super().__init__(cursor)
        self._rowcount = cursor.rowcount
        self._rows = (
            [Row(self._names, tuple(values)) for values in cursor.fetchall()]
            if self._names
            else []
        )

    @property
    def rowcount(self) -> int:
        return self._rowcount

    def fetchone(self) -> Row | None:
        return self._rows.pop(0) if self._rows else None

    def fetchall(self) -> list[Row]:
        rows, self._rows = self._rows, []
        return rows


def pool_conninfo(dsn: str) -> str:
    """The DSN with a statement timeout added to (never replacing) its options.

    A pool ``kwargs`` ``options`` entry would override the DSN's own options,
    such as a ``search_path``, so the timeout is merged into the DSN instead.
    """

    parameters = conninfo_to_dict(dsn)
    options = str(parameters.get("options") or "").strip()
    parameters["options"] = f"{options} -c statement_timeout=15000".strip()
    return make_conninfo(**{key: str(value) for key, value in parameters.items()})


def open_pool(dsn: str, *, max_size: int = 10) -> ConnectionPool[psycopg.Connection[Any]]:
    """A small pool; statements are short and the store keeps no session state."""

    return ConnectionPool(
        pool_conninfo(dsn),
        min_size=1,
        max_size=max_size,
        timeout=10.0,
        kwargs={"autocommit": False},
        open=True,
    )


__all__ = [
    "CONTROL_WRITE_LOCK_KEY",
    "DbConnection",
    "DbCursor",
    "IntegrityError",
    "PostgresConnection",
    "Row",
    "open_pool",
    "translate_sql",
]
