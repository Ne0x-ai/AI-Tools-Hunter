"""Daemon liveness: one row that says "the ingest loop is still running".

The architecture has exactly one writer for CT ingestion — the ``start``
process — and ``serve`` must not pretend to be one. But ``serve`` still has to
answer a question that only ingest can answer: *is anything ever going to enter
the queue?* Without this table the console's "nothing to do" and "the daemon is
not running" look identical.

This is a read-mostly, write-rarely table: ``start`` touches it at least once
per cycle (and every 50 pages inside a long sweep), everything else only reads.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ai_tools_before_launch.storage.database import ConnectionFactory

HEARTBEAT_ID = 1

DAEMON_STALE_SECONDS = 300.0
"""How long a heartbeat stays trustworthy.

``start`` bumps the row at each cycle *and* every 50 pages during a sweep, so a
healthy daemon touches it far more often than this even while consuming a
million entries. Five minutes therefore means "the process is gone", not "the
process is busy".
"""

_UPSERT = """
    INSERT INTO daemon_heartbeat (
        id, cycle, updated_at, entries_behind, processed_lag_seconds, head_lag_seconds
    ) VALUES (1, ?, ?, ?, ?, ?)
    ON CONFLICT(id) DO UPDATE SET
        cycle = COALESCE(excluded.cycle, daemon_heartbeat.cycle),
        updated_at = excluded.updated_at,
        entries_behind = COALESCE(excluded.entries_behind, daemon_heartbeat.entries_behind),
        processed_lag_seconds = COALESCE(
            excluded.processed_lag_seconds, daemon_heartbeat.processed_lag_seconds
        ),
        head_lag_seconds = COALESCE(
            excluded.head_lag_seconds, daemon_heartbeat.head_lag_seconds
        )
"""
"""Liveness bump and measurement share one statement.

A bare "still alive" touch passes ``None`` for the measurements and must not
erase the last real reading — hence ``COALESCE`` and not a plain assignment.
"""


@dataclass(frozen=True, slots=True)
class DaemonHeartbeat:
    """The last known state of the ingest daemon."""

    cycle: int | None
    updated_at: datetime
    entries_behind: int | None
    processed_lag_seconds: float | None
    head_lag_seconds: float | None

    def age_seconds(self, *, now: datetime) -> float:
        return (now - self.updated_at).total_seconds()

    def is_alive(self, *, now: datetime) -> bool:
        return self.age_seconds(now=now) <= DAEMON_STALE_SECONDS


class DaemonStore:
    """Liveness of the ingest daemon, as seen from either process."""

    _connection: ConnectionFactory

    def record_daemon_heartbeat(
        self,
        *,
        at: datetime,
        cycle: int | None = None,
        entries_behind: int | None = None,
        processed_lag_seconds: float | None = None,
        head_lag_seconds: float | None = None,
    ) -> None:
        """Record that the ingest loop is alive, optionally with its lag.

        Measurement fields are only overwritten when supplied, so the frequent
        liveness bumps during a long sweep cannot blank out the last real
        numbers the console shows.
        """
        if at.tzinfo is None:
            raise ValueError("at must be timezone-aware")
        with self._connection() as connection:
            connection.execute(
                _UPSERT,
                (
                    cycle,
                    at.isoformat(),
                    entries_behind,
                    processed_lag_seconds,
                    head_lag_seconds,
                ),
            )

    def read_daemon_heartbeat(self) -> DaemonHeartbeat | None:
        """Return the last heartbeat, or ``None`` if ``start`` never ran."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT cycle, updated_at, entries_behind, processed_lag_seconds,"
                " head_lag_seconds FROM daemon_heartbeat WHERE id = ?",
                (HEARTBEAT_ID,),
            ).fetchone()
        if row is None:
            return None
        return DaemonHeartbeat(
            cycle=row["cycle"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
            entries_behind=row["entries_behind"],
            processed_lag_seconds=row["processed_lag_seconds"],
            head_lag_seconds=row["head_lag_seconds"],
        )
