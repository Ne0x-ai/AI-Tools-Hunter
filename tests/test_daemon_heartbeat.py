"""The one-row liveness record that lets the console trust an empty queue."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from ai_tools_before_launch.storage.daemon import DAEMON_STALE_SECONDS
from ai_tools_before_launch.storage.sqlite import SQLiteStore

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def test_a_database_the_daemon_never_touched_has_no_heartbeat(tmp_path) -> None:
    assert SQLiteStore(tmp_path / "cold.db").read_daemon_heartbeat() is None


def test_liveness_bumps_do_not_erase_the_last_measurement(tmp_path) -> None:
    """A sweep touches the row every 50 pages; measurements must survive that.

    The console shows ``processed_lag_seconds``; blanking it on every liveness
    bump would leave the number permanently empty during exactly the long
    catch-up runs where it matters most.
    """
    store = SQLiteStore(tmp_path / "coalesce.db")
    store.record_daemon_heartbeat(
        at=NOW,
        cycle=4,
        entries_behind=250,
        processed_lag_seconds=640.0,
        head_lag_seconds=30.0,
    )

    store.record_daemon_heartbeat(at=NOW + timedelta(seconds=30), cycle=4)

    heartbeat = store.read_daemon_heartbeat()
    assert heartbeat is not None
    assert heartbeat.entries_behind == 250
    assert heartbeat.processed_lag_seconds == 640.0
    assert heartbeat.head_lag_seconds == 30.0
    assert heartbeat.updated_at == NOW + timedelta(seconds=30)


def test_the_heartbeat_is_exactly_one_row(tmp_path) -> None:
    database = tmp_path / "single.db"
    store = SQLiteStore(database)
    for cycle in range(1, 6):
        store.record_daemon_heartbeat(at=NOW + timedelta(minutes=cycle), cycle=cycle)

    connection = sqlite3.connect(database)
    try:
        rows = connection.execute("SELECT COUNT(*) FROM daemon_heartbeat").fetchone()[0]
    finally:
        connection.close()

    assert rows == 1
    assert store.read_daemon_heartbeat().cycle == 5


def test_freshness_decides_whether_the_daemon_is_alive(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "alive.db")
    store.record_daemon_heartbeat(at=NOW)

    heartbeat = store.read_daemon_heartbeat()
    assert heartbeat is not None
    assert heartbeat.is_alive(now=NOW)
    assert heartbeat.is_alive(now=NOW + timedelta(seconds=DAEMON_STALE_SECONDS - 1))
    assert not heartbeat.is_alive(now=NOW + timedelta(seconds=DAEMON_STALE_SECONDS + 1))


def test_an_unreadable_measurement_is_not_written_as_zero(tmp_path) -> None:
    """``None`` means "we could not read the log", not "we are caught up"."""
    store = SQLiteStore(tmp_path / "unknown.db")
    store.record_daemon_heartbeat(at=NOW, cycle=1)

    heartbeat = store.read_daemon_heartbeat()
    assert heartbeat is not None
    assert heartbeat.entries_behind is None
    assert heartbeat.processed_lag_seconds is None


def test_a_naive_timestamp_is_rejected(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "naive.db")
    with pytest.raises(ValueError):
        store.record_daemon_heartbeat(at=datetime(2026, 11, 1, 12, 0))
