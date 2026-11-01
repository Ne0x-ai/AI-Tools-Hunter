"""Triggering and observing CT discovery.

The console may observe and drain. It may **not** ingest.

CT ingestion has exactly one writer — the ``start`` daemon — because two
processes appending to ``source_events`` and advancing the same cursor is a
data race with a friendly name. What the console can do safely is drain the
lease-protected work queue, because the lease *is* the concurrency primitive:
a claim is an atomic ``BEGIN IMMEDIATE``, and retry/complete only apply while
the caller still holds the token.

So ``POST /v1/run/discovery`` means "probe what the daemon already queued", and
the response says honestly whether there was anything to probe and whether a
daemon is even alive to queue more.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from ai_tools_before_launch.api.dependencies import StoreDep
from ai_tools_before_launch.crawler.http_probe import HTTPProbe
from ai_tools_before_launch.filter.pipeline import FilterPipeline
from ai_tools_before_launch.ingest.ct_orchestrator import CTIngestOrchestrator
from ai_tools_before_launch.ingest.ct_poller import CT_SOURCE_NAME
from ai_tools_before_launch.pipeline import AIToolsBeforeLaunchPipeline
from ai_tools_before_launch.storage.daemon import DAEMON_STALE_SECONDS

router = APIRouter(prefix="/v1")

_LOG_TAIL_BYTES = 262_144


class DiscoveryRunRequest(BaseModel):
    max_probes: int = Field(default=20, ge=1, le=200)


def _daemon_status(store: StoreDep, *, now: datetime) -> dict[str, object]:
    """Whether the ingest daemon is alive, and what it last measured.

    ``running: False`` is not an error — it explains an empty queue. Without it,
    "0 candidates" is indistinguishable from "nobody is collecting anything",
    which is exactly the confusion the single-writer split could introduce.
    """
    heartbeat = store.read_daemon_heartbeat()
    if heartbeat is None:
        return {
            "running": False,
            "stale_after_seconds": DAEMON_STALE_SECONDS,
            "hint": "start has never run against this database; nothing will queue",
        }
    return {
        "running": heartbeat.is_alive(now=now),
        "last_seen_at": heartbeat.updated_at.isoformat(),
        "age_seconds": round(heartbeat.age_seconds(now=now), 1),
        "stale_after_seconds": DAEMON_STALE_SECONDS,
        "cycle": heartbeat.cycle,
        "entries_behind": heartbeat.entries_behind,
        "processed_lag_seconds": heartbeat.processed_lag_seconds,
        "head_lag_seconds": heartbeat.head_lag_seconds,
    }


@router.post("/run/discovery")
async def run_discovery(
    payload: DiscoveryRunRequest, store: StoreDep
) -> dict[str, object]:
    """Drain due work from the queue; the console never reads CT itself."""
    now = datetime.now(UTC)
    daemon = _daemon_status(store, now=now)
    due_work = store.due_ct_discovery_work_count(now=now)
    try:
        async with HTTPProbe() as probe:
            pipeline = AIToolsBeforeLaunchPipeline(store=store, probe=probe)
            strict_filter = FilterPipeline(
                tier1_days=30,
                tier2_days=90,
                require_dns=True,
                drop_unknown_rdap=True,
            )
            orchestrator = CTIngestOrchestrator(
                store=store,
                pipeline=pipeline,
                filter_pipeline=strict_filter,
                require_first_seen=True,
                probe_limit=payload.max_probes,
            )
            summary = await orchestrator.run_once(poll=False)
    except Exception as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"discovery run failed: {error}",
        ) from error
    if summary.candidates_created > 0:
        outcome = "completed"
    elif due_work == 0 and summary.pending_work == 0:
        outcome = "no_work"
    elif summary.pending_work > 0:
        outcome = "queued"
    else:
        outcome = "no_candidates"
    return {
        "status": outcome,
        "due_work": due_work,
        "pending_work": summary.pending_work,
        "probes_run": summary.probes_run,
        "candidates_created": summary.candidates_created,
        "strict_rejections": summary.strict_rejections,
        "daemon": daemon,
    }


@router.get("/discovery/log")
def discovery_log(request: Request, tail: int = 200) -> dict[str, object]:
    """Return the tail of the discover daemon's log file (sibling of the DB)."""
    tail = max(1, min(tail, 1000))
    log_path: Path = request.app.state.discovery_log_path
    if not log_path.exists():
        return {"exists": False, "lines": []}
    with log_path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - _LOG_TAIL_BYTES))
        chunk = handle.read().decode("utf-8", errors="replace")
    return {"exists": True, "lines": chunk.splitlines()[-tail:]}


@router.get("/discovery/overview")
def discovery_overview(store: StoreDep) -> dict[str, object]:
    """Return a compact view of discovered domains and their latest probe state."""
    metrics = store.funnel_metrics()
    domains = [
        {
            "domain": snapshot.domain,
            "first_seen_at": (
                snapshot.first_seen_at.isoformat()
                if snapshot.first_seen_at is not None
                else None
            ),
            "observed_at": (
                snapshot.observed_at.isoformat()
                if snapshot.observed_at is not None
                else None
            ),
            "outcome_code": snapshot.outcome_code,
            "attempt_number": snapshot.attempt_number,
            "status_code": snapshot.status_code,
            "final_url": snapshot.final_url,
            "detail": snapshot.detail,
        }
        for snapshot in store.domain_snapshots()
    ]
    return {
        "cursor": store.get_source_cursor(CT_SOURCE_NAME),
        "domains": domains,
        "daemon": _daemon_status(store, now=datetime.now(UTC)),
        "counts": {
            "source_events": metrics.source_events,
            "domains": metrics.domains,
            "observations": metrics.observations,
            "candidates": metrics.candidates,
            "candidate_versions": metrics.candidate_versions,
            "review_queue": len(store.list_review_queue()),
        },
    }
