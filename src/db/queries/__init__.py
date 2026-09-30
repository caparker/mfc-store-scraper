"""Named read-only SQL queries for the `query` CLI command.

Each `.sql` file in this directory is a plain SELECT (no trailing semicolon).
The file name, minus extension, is the query name. A leading `-- ` comment
on the first line is used as the description in `query --list`.
"""

import csv
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from rich.console import Console
from rich.table import Table

from src.db.postgres import Database

QUERY_DIRECTORY = Path(__file__).resolve().parent

DEFAULT_LIMIT = 100


@dataclass(frozen=True)
class NamedQuery:
    name: str
    description: str
    body: str


def list_queries() -> list[NamedQuery]:
    """All named queries, sorted by name."""
    queries = []
    for path in sorted(QUERY_DIRECTORY.glob("*.sql")):
        text = path.read_text(encoding="utf-8").strip().rstrip(";")
        first_line = text.splitlines()[0] if text else ""
        description = first_line[2:].strip() if first_line.startswith("--") else ""
        queries.append(NamedQuery(name=path.stem, description=description, body=text))
    return queries


def load_query(name: str) -> NamedQuery:
    """Load one named query; raises KeyError if it does not exist."""
    for query in list_queries():
        if query.name == name:
            return query
    raise KeyError(name)


def build_sql(
    base: str,
    where: str | None = None,
    order_by: str | None = None,
    limit: int | None = None,
) -> str:
    """Wrap `base` as a subquery so the clauses apply to its output columns."""
    parts = [f"SELECT * FROM (\n{base}\n) q"]
    if where:
        parts.append(f"WHERE {where}")
    if order_by:
        parts.append(f"ORDER BY {order_by}")
    if limit is not None:
        parts.append(f"LIMIT {int(limit)}")
    return "\n".join(parts)


def run_readonly(statement: str) -> tuple[list[str], list[dict[str, Any]]]:
    """Run `statement` in a read-only transaction; return (columns, rows)."""
    with Database().connection() as conn:
        conn.read_only = True
        with conn.cursor(row_factory=dict_row) as curs:
            curs.execute(sql.SQL(statement))  # type: ignore[arg-type]
            columns = [d.name for d in curs.description or []]
            rows = curs.fetchall()
    return columns, rows


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)
    return str(value)


def print_table(columns: list[str], rows: list[dict[str, Any]]) -> None:
    # One line per row. If the table is wider than the terminal, print it at
    # its natural width and let the terminal wrap rather than squash columns.
    table = Table()
    for col in columns:
        table.add_column(col, no_wrap=True)
    for row in rows:
        table.add_row(*(_cell(row[c]) for c in columns))
    console = Console()
    # Measure against an unbounded console; Table clamps its measurement to
    # the console width otherwise.
    natural_width = Console(width=100_000).measure(table).maximum
    if natural_width > console.width:
        console = Console(width=natural_width)
    console.print(table, crop=False)


def print_csv(columns: list[str], rows: list[dict[str, Any]]) -> None:
    writer = csv.writer(sys.stdout)
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_cell(row[c]) for c in columns])


def print_json(columns: list[str], rows: list[dict[str, Any]]) -> None:
    json.dump(
        [{c: row[c] for c in columns} for row in rows],
        sys.stdout,
        indent=2,
        default=str,
    )
    sys.stdout.write("\n")


PRINTERS = {
    "table": print_table,
    "csv": print_csv,
    "json": print_json,
}

QueryError = psycopg.Error
ReadOnlyViolation = psycopg.errors.ReadOnlySqlTransaction
