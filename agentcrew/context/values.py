"""Column profiling for the shared context, done once at startup.

* Low-cardinality text columns (fewer than `max_distinct` distinct values)
  are listed with every real value, so a model never has to guess spelling
  or case.
* Date-like text columns ('YYYY-MM-DD' or 'YYYY-MM') are summarised as a
  format and a min..max range instead, which is also the data's date range.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import Engine, text

from agentcrew.db.introspect import Catalog

MAX_DISTINCT = 200
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_MONTH = re.compile(r"\d{4}-\d{2}")


@dataclass(frozen=True)
class ColumnValues:
    table: str
    column: str
    values: tuple[str, ...]


@dataclass(frozen=True)
class DateRange:
    table: str
    column: str
    fmt: str
    minimum: str
    maximum: str


@dataclass(frozen=True)
class Profile:
    values: tuple[ColumnValues, ...]
    dates: tuple[DateRange, ...]

    def render_values(self) -> str:
        return "\n".join(
            f"{v.table}.{v.column}: " + ", ".join(f"'{x}'" for x in v.values)
            for v in self.values
        )

    def render_dates(self) -> str:
        return "\n".join(
            f"{d.table}.{d.column} ({d.fmt}): {d.minimum} .. {d.maximum}"
            for d in self.dates
        )


def _date_format(sample: str) -> str | None:
    if _DATE.fullmatch(sample):
        return "YYYY-MM-DD"
    if _MONTH.fullmatch(sample):
        return "YYYY-MM"
    return None


def profile_columns(
    engine: Engine, catalog: Catalog, *, max_distinct: int = MAX_DISTINCT
) -> Profile:
    values: list[ColumnValues] = []
    dates: list[DateRange] = []
    with engine.connect() as conn:
        for table_name in sorted(catalog.tables):
            for col in catalog.tables[table_name].columns:
                if "TEXT" not in col.type.upper() and "CHAR" not in col.type.upper():
                    continue
                # Names come from introspection of this database, not input.
                c, t = f'"{col.name}"', f'"{table_name}"'
                present = f"FROM {t} WHERE {c} IS NOT NULL"
                distinct = conn.execute(
                    text(f"SELECT COUNT(DISTINCT {c}) FROM {t}")  # noqa: S608
                ).scalar_one()
                sample = conn.execute(
                    text(f"SELECT {c} {present} LIMIT 1")  # noqa: S608
                ).scalar()
                fmt = _date_format(str(sample)) if sample is not None else None
                if fmt:
                    lo, hi = conn.execute(
                        text(f"SELECT MIN({c}), MAX({c}) FROM {t}")  # noqa: S608
                    ).one()
                    dates.append(DateRange(table_name, col.name, fmt, lo, hi))
                elif distinct < max_distinct:
                    rows = conn.execute(
                        text(f"SELECT DISTINCT {c} {present} ORDER BY 1")  # noqa: S608
                    ).scalars()
                    values.append(ColumnValues(table_name, col.name, tuple(rows)))
    return Profile(values=tuple(values), dates=tuple(dates))
