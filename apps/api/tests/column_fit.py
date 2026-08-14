"""Assert that a persisted row would survive a column-typed database.

The default test database is in-memory SQLite, which enforces neither ``VARCHAR(n)`` lengths
nor ``NUMERIC(p, s)`` ranges: it stores whatever it is handed. PostgreSQL, which every
deployment actually runs on, rejects both with ``value too long for type character varying(n)``
and ``numeric field overflow``. That asymmetry is what let two overflow bugs reach production
while the whole suite stayed green.

This module closes the gap from the SQLite side, so a regression is caught by the ordinary
``pytest`` run rather than only by the opt-in PostgreSQL suite in
``test_postgres_column_bounds.py``. It reads the limits off the mapped columns, so widening or
narrowing a column moves these checks with it instead of leaving a stale constant behind.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import Numeric, String
from sqlalchemy.inspection import inspect


def column_fit_violations(row: Any) -> list[str]:
    """Every value on ``row`` that its own column could not physically hold.

    Returns human-readable strings rather than raising, so a caller can sweep a whole merge and
    report every offending column at once instead of stopping at the first.
    """
    violations: list[str] = []
    mapper = inspect(type(row))
    label = type(row).__name__

    for column in mapper.columns:
        value = getattr(row, column.key, None)
        if value is None:
            continue
        coltype = column.type

        if isinstance(coltype, String) and isinstance(value, str):
            limit = coltype.length
            if limit is not None and len(value) > limit:
                violations.append(
                    f"{label}.{column.key}: {len(value)} chars exceeds VARCHAR({limit})"
                )

        elif isinstance(coltype, Numeric) and isinstance(value, Decimal):
            if not value.is_finite():
                violations.append(f"{label}.{column.key}: non-finite Decimal({value!r})")
                continue
            precision, scale = coltype.precision, coltype.scale
            if precision is None or scale is None:
                continue
            # NUMERIC(p, s) holds p - s integer digits and s fractional digits. Compare against
            # the normalised exponent so trailing zeros do not read as excess precision.
            exponent = value.as_tuple().exponent
            assert isinstance(exponent, int)  # finite, so never 'n'/'N'/'F'
            if -exponent > scale:
                violations.append(
                    f"{label}.{column.key}: {value} needs scale {-exponent} > {scale}"
                )
            if abs(value) >= Decimal(10) ** (precision - scale):
                violations.append(
                    f"{label}.{column.key}: {value} exceeds NUMERIC({precision}, {scale}) range"
                )

    return violations


def assert_fits_columns(*rows: Any) -> None:
    """Fail with every violating column named, or pass silently."""
    violations = [v for row in rows for v in column_fit_violations(row)]
    assert not violations, "values a column-typed database would reject:\n  " + "\n  ".join(
        violations
    )
