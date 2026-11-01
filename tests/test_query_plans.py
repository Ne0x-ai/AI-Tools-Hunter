"""Query-plan guards: the reads that decide whether the console scales.

Correctness tests pass on a 10-row database and on a 5-million-row one. These
do not — they assert the *plan*, because the failure mode here is not a wrong
answer but a full table scan per domain. Measured on 50k domains / 150k
observations: 86 seconds with no index, 0.19 seconds with one.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from ai_tools_before_launch.domain.observations import Observation, OutcomeCode
from ai_tools_before_launch.storage.sqlite import DOMAIN_SNAPSHOT_SQL, SQLiteStore


def _seed(store: SQLiteStore, database, domains: int = 50, per_domain: int = 3) -> None:
    now = datetime.now(UTC)
    connection = sqlite3.connect(database)
    try:
        connection.executemany(
            "INSERT OR IGNORE INTO domains (domain, first_seen_at) VALUES (?, ?)",
            [(f"alpha-{index}.com", now.isoformat()) for index in range(domains)],
        )
        connection.commit()
    finally:
        connection.close()
    for index in range(domains):
        domain = f"alpha-{index}.com"
        for attempt in range(1, per_domain + 1):
            store.append_observation(
                Observation(
                    domain=domain,
                    outcome_code=OutcomeCode.SUCCESS,
                    observed_at=now,
                    attempt_number=attempt,
                    status_code=200,
                    final_url=f"https://{domain}/",
                    detail=None,
                )
            )


def _plan(database, sql: str) -> str:
    connection = sqlite3.connect(database)
    try:
        rows = connection.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
    finally:
        connection.close()
    return " | ".join(row[-1] for row in rows)


def test_the_overview_query_seeks_per_domain_instead_of_scanning(tmp_path) -> None:
    """The correlated subquery must resolve by index, not by scanning.

    ``SCAN latest`` in the plan means every domain re-reads the whole
    observations table. That is O(domains x observations) — invisible at test
    scale, fatal on a real database.
    """
    database = tmp_path / "plan.db"
    store = SQLiteStore(database)
    _seed(store, database)

    plan = _plan(database, DOMAIN_SNAPSHOT_SQL)

    assert "SCAN latest" not in plan, f"overview regressed to a scan: {plan}"
    assert "idx_observations_domain" in plan, f"the index is not being used: {plan}"


def test_the_observations_index_exists_in_a_fresh_database(tmp_path) -> None:
    database = tmp_path / "fresh.db"
    SQLiteStore(database)

    connection = sqlite3.connect(database)
    try:
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='observations'"
            )
        }
    finally:
        connection.close()

    assert "idx_observations_domain" in indexes


def test_an_existing_database_gains_the_index_when_reopened(tmp_path) -> None:
    """``_SCHEMA`` runs on every open, so an upgrade needs no migration code.

    Anyone who has been running this for weeks has a database without the
    index; they must not have to rebuild it.
    """
    database = tmp_path / "legacy.db"
    SQLiteStore(database)

    connection = sqlite3.connect(database)
    try:
        connection.execute("DROP INDEX idx_observations_domain")
        connection.commit()
    finally:
        connection.close()

    SQLiteStore(database)

    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_observations_domain'"
        ).fetchone() is not None
    finally:
        connection.close()


def test_domain_snapshots_still_returns_every_domain_once(tmp_path) -> None:
    """The index must not change the answer — only the cost."""
    store = SQLiteStore(tmp_path / "answers.db")
    _seed(store, tmp_path / "answers.db", domains=5, per_domain=4)

    snapshots = store.domain_snapshots()

    assert [item.domain for item in snapshots] == [f"alpha-{i}.com" for i in range(5)]
    assert all(item.attempt_number == 4 for item in snapshots), "latest observation won"
