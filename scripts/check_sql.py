"""Check the SQL in bot/db.py against schema.sql.

Run:  python scripts/check_sql.py

Every statement is parsed with libpg_query — the real PostgreSQL grammar shipped
as a Python extension — so a typo or a syntax error fails here instead of at
3am on the VPS. It then walks each parse tree for the tables the statement
touches and checks they exist in schema.sql.

It cannot prove semantics (column types, constraint behaviour) — tests/test_db_live.py
covers that against a real database.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

try:
    from pglast import parse_sql
    from pglast.visitors import Visitor
except ImportError:  # pragma: no cover
    sys.exit("pglast is required: pip install -r requirements-dev.txt")

ROOT = Path(__file__).resolve().parent.parent

SQL_START = re.compile(
    r"^(select|insert|update|delete|with|alter|create|drop|begin|commit|grant|set)\b",
    re.IGNORECASE,
)
# $1 placeholders are only legal inside PREPARE, so swap them for NULL before
# parsing. Only ever applied to the query strings lifted out of db.py: a .sql
# file has no bind parameters, and substituting there would corrupt any
# dollar-quoted literal that happens to start with a digit - the clinical bank
# has an explanation opening "400 mg in 48 kg ...", which $q$400 turns into
# $qNULL and an unterminated quote.
PLACEHOLDER = re.compile(r"\$\d+")


class SqlExtractor(ast.NodeVisitor):
    """Collect SQL string literals, including f-strings with the substitutions
    replaced by a harmless identifier."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def _maybe_add(self, text: str) -> None:
        if SQL_START.match(text.strip().lstrip("(").strip()):
            self.statements.append(text)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            self._maybe_add(node.value)

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                parts.append("placeholder")
        self._maybe_add("".join(parts))
        # deliberately not recursing: the fragments are not statements on their own


class TableCollector(Visitor):
    """Tables a statement touches, excluding CTE names defined in the same query."""

    def __init__(self) -> None:
        self.tables: set[str] = set()
        self.ctes: set[str] = set()

    def visit_CommonTableExpr(self, _ancestors, node) -> None:
        if node.ctename:
            self.ctes.add(node.ctename.lower())

    def visit_RangeVar(self, _ancestors, node) -> None:
        if node.relname:
            self.tables.add(node.relname.lower())

    @property
    def external(self) -> set[str]:
        return self.tables - self.ctes


def statements_from(path: Path) -> list[str]:
    if path.suffix == ".sql":
        return [path.read_text()]
    extractor = SqlExtractor()
    extractor.visit(ast.parse(path.read_text()))
    return extractor.statements


def main() -> int:
    files = [
        ROOT / "schema.sql",
        *(ROOT / "migrations").glob("*.sql"),
        *(ROOT / "seeds").glob("*.sql"),
        ROOT / "bot" / "db.py",
    ]

    failures = 0
    parsed = 0
    referenced: set[str] = set()

    for path in files:
        for i, statement in enumerate(statements_from(path), start=1):
            if not statement.strip():
                continue
            sql = (statement if path.suffix == ".sql"
                   else PLACEHOLDER.sub("NULL", statement))
            try:
                trees = parse_sql(sql)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL  {path.name} statement {i}: {exc}")
                print(f"      {statement.strip()[:160]!r}")
                continue
            parsed += 1
            if path.name == "db.py":
                collector = TableCollector()
                collector(trees)
                referenced |= collector.external

    schema_text = (ROOT / "schema.sql").read_text().lower()
    created = set(re.findall(r"create table (?:if not exists )?([a-z_][a-z0-9_]*)", schema_text))

    missing = sorted(referenced - created)
    if missing:
        failures += len(missing)
        print(f"FAIL  bot/db.py uses tables that schema.sql does not create: {missing}")

    print(f"{parsed} statement(s) parsed fine")
    print(f"{len(created)} table(s) in schema.sql, {len(referenced)} referenced by db.py")
    if failures:
        print(f"{failures} problem(s) found")
        return 1
    print("SQL parses, and the code only touches tables the schema creates.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
