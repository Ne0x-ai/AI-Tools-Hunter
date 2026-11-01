"""Tests for the web discovery API surface.

The contract changed with the single-writer split: ``POST /v1/run/discovery``
means "drain what the daemon queued", not "go read CT logs". These tests pin
both halves — what the endpoint does, and what it must never be able to do.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from ai_tools_before_launch.api import create_app, discovery
from ai_tools_before_launch.ingest.ct_orchestrator import CTIngestRunSummary
from ai_tools_before_launch.storage.sqlite import SQLiteStore


def _summary(**overrides) -> CTIngestRunSummary:
    payload = {
        "certificates_seen": 0,
        "events_added": 0,
        "probes_run": 0,
        "candidates_created": 0,
        "next_cursor": None,
    }
    payload.update(overrides)
    return CTIngestRunSummary(**payload)


class _CapturingOrchestrator:
    """Records how the endpoint constructed and drove the orchestrator."""

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        captured["init"] = kwargs
        captured["run_once"] = []

    async def run_once(self, *, poll: bool = True) -> CTIngestRunSummary:
        captured["run_once"].append({"poll": poll})
        return captured["summary"]


captured: dict[str, object] = {}


def _install(monkeypatch, summary: CTIngestRunSummary) -> None:
    captured.clear()
    captured["summary"] = summary
    monkeypatch.setattr(discovery, "CTIngestOrchestrator", _CapturingOrchestrator)


def test_discovery_overview_empty(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "discovery.db"))
    response = client.get("/v1/discovery/overview")
    assert response.status_code == 200
    payload = response.json()
    assert payload["domains"] == []
    assert payload["counts"]["source_events"] == 0
    assert payload["daemon"]["running"] is False


def test_legacy_dashboard_routes_redirect_to_inbox(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "legacy-dashboard.db"))

    for path in ("/discovery", "/ops"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/"


def test_discovery_run_validates_max_probes(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "discovery-validation.db"))
    response = client.post("/v1/run/discovery", json={"max_probes": 0})
    assert response.status_code == 422


def test_the_console_never_reads_ct_logs(monkeypatch, tmp_path) -> None:
    """Ingest has one writer. The API module must not even link the adapter.

    Asserting on behaviour alone would miss the day someone re-adds polling
    behind a flag; asserting on the import list makes the boundary structural.
    """
    source = inspect.getsource(discovery)

    assert "CTLogFetcher" not in source
    assert "CTPoller(" not in source
    assert "DEFAULT_LOG" not in source


def test_web_discovery_drains_the_queue_without_polling(monkeypatch, tmp_path) -> None:
    """``poll=False`` and no poller: the two facts that make this safe."""
    _install(monkeypatch, _summary(probes_run=3, candidates_created=1))

    response = TestClient(create_app(tmp_path / "drain.db")).post(
        "/v1/run/discovery", json={"max_probes": 5}
    )

    assert response.status_code == 200
    assert captured["run_once"] == [{"poll": False}]
    assert "poller" not in captured["init"], "a poller would let the console ingest"


def test_web_discovery_requires_verified_age_and_dns_before_l1(
    monkeypatch, tmp_path
) -> None:
    """The browser action must opt into the strict CT gate before probing."""
    _install(monkeypatch, _summary(probes_run=3, candidates_created=1))

    response = TestClient(create_app(tmp_path / "strict.db")).post(
        "/v1/run/discovery", json={"max_probes": 5}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["probes_run"] == 3
    assert body["candidates_created"] == 1
    # The console cannot report ingest numbers, because it does not ingest.
    assert "certificates_seen" not in body
    assert "next_cursor" not in body

    strict_filter = captured["init"]["filter_pipeline"]
    assert strict_filter._drop_unknown_rdap is True
    assert strict_filter._require_dns is True
    assert captured["init"]["require_first_seen"] is True


def test_web_discovery_reports_no_work_and_a_dead_daemon(monkeypatch, tmp_path) -> None:
    """"Nothing to do" and "nobody is collecting" must not look the same."""
    _install(monkeypatch, _summary())

    response = TestClient(create_app(tmp_path / "idle.db")).post(
        "/v1/run/discovery", json={"max_probes": 5}
    )

    body = response.json()
    assert body["status"] == "no_work"
    assert body["due_work"] == 0
    assert body["daemon"]["running"] is False
    assert "hint" in body["daemon"]


def test_web_discovery_reports_queued_work(monkeypatch, tmp_path) -> None:
    """Unfinished work is not an empty result."""
    _install(monkeypatch, _summary(probes_run=2, pending_work=4))

    response = TestClient(create_app(tmp_path / "queued.db")).post(
        "/v1/run/discovery", json={"max_probes": 5}
    )

    body = response.json()
    assert body["status"] == "queued"
    assert body["pending_work"] == 4


def test_web_discovery_reports_a_live_daemon(monkeypatch, tmp_path) -> None:
    """A fresh heartbeat is what turns "empty" into "caught up"."""
    database = tmp_path / "alive.db"
    store = SQLiteStore(database)
    store.record_daemon_heartbeat(
        at=datetime.now(UTC),
        cycle=7,
        entries_behind=0,
        processed_lag_seconds=31.5,
        head_lag_seconds=9.0,
    )
    _install(monkeypatch, _summary())

    response = TestClient(create_app(database)).post(
        "/v1/run/discovery", json={"max_probes": 5}
    )

    daemon = response.json()["daemon"]
    assert daemon["running"] is True
    assert daemon["cycle"] == 7
    assert daemon["processed_lag_seconds"] == 31.5


def test_web_discovery_reports_a_stale_daemon(monkeypatch, tmp_path) -> None:
    database = tmp_path / "stale.db"
    store = SQLiteStore(database)
    store.record_daemon_heartbeat(
        at=datetime.now(UTC) - timedelta(hours=1), cycle=3
    )
    _install(monkeypatch, _summary())

    response = TestClient(create_app(database)).post(
        "/v1/run/discovery", json={"max_probes": 5}
    )

    daemon = response.json()["daemon"]
    assert daemon["running"] is False
    assert daemon["cycle"] == 3
    assert daemon["age_seconds"] >= 3600


def test_discovery_log_endpoint_returns_daemon_tail(tmp_path) -> None:
    database = tmp_path / "dh.db"
    log_file = tmp_path / "dh.log"
    log_file.write_text(
        "\n".join(f"line-{i}" for i in range(300)) + "\n", encoding="utf-8"
    )

    client = TestClient(create_app(database))
    response = client.get("/v1/discovery/log", params={"tail": 5})

    assert response.status_code == 200
    body = response.json()
    assert body["exists"] is True
    assert body["lines"] == [f"line-{i}" for i in range(295, 300)]


def test_discovery_log_endpoint_when_daemon_never_ran(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "fresh.db"))
    response = client.get("/v1/discovery/log")

    assert response.status_code == 200
    assert response.json() == {"exists": False, "lines": []}
