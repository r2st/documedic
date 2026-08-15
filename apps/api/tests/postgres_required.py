"""How the PostgreSQL-only tests decide between skipping and failing.

Three modules in this suite need a real PostgreSQL — the migration chain, the migration data
reconciliation, and the column-bound overflows — because the thing they check does not exist on
SQLite. Each of them skips when no server is reachable, which is right for a laptop and is how
they have always run.

It is also how two of them went stale without anyone noticing. ``test_migration_chain_postgres``
asserted on the column revision *0020* adds and kept asserting it through 0028, so eight
revisions shipped with their rollback unverified. ``test_postgres_column_bounds`` compared the
merge's return value to a dict literal that stopped being complete the moment encounters became
a merged entity type. Both were green the whole time, because a skipped test is a green test and
nothing in the run said which guarantees had been bought and which had been skipped past.

The env var is the fix a skip-by-default design needs: somewhere that a PostgreSQL is *meant* to
be reachable — CI, a release check, a developer who has just run ``docker compose up -d
postgres`` and wants to know these actually ran — set ``REQUIRE_POSTGRES=1`` and an unreachable
server becomes a failure with the connection error attached, instead of a silent pass.

    docker compose up -d postgres
    REQUIRE_POSTGRES=1 pytest -m postgres
"""

from __future__ import annotations

import os

import pytest

ENV_VAR = "REQUIRE_POSTGRES"
_OFF = {"", "0", "false", "no", "off"}


def required() -> bool:
    """True when the caller has declared that a PostgreSQL must be reachable."""
    return os.environ.get(ENV_VAR, "").strip().lower() not in _OFF


def unavailable(reason: str) -> None:
    """Skip this test for want of a PostgreSQL — or fail it, if one was required.

    Never returns.
    """
    if required():
        pytest.fail(
            f"{ENV_VAR} is set, so these tests must run rather than skip, and no PostgreSQL "
            f"was reachable: {reason}"
        )
    pytest.skip(reason)
