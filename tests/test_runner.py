"""Tests for the manual CT backfill: start-index search and bounded fan-out."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ai_tools_before_launch.filter.pipeline import FilterPipeline
from ai_tools_before_launch.filter.rdap_age import Registration
from ai_tools_before_launch.ingest.runner import StartConfig, locate_start_index


class _FakeTimestampFetcher:
    """Answers tree_sizes/leaf_timestamp from a monotonic timestamp table."""

    def __init__(self, stamps: dict[str, list[datetime]]) -> None:
        self._stamps = stamps

    @property
    def logs(self):  # noqa: ANN201 - duck-typed for the search
        return tuple(
            type("_Log", (), {"log_id": log_id})() for log_id in self._stamps
        )

    async def tree_sizes(self) -> dict[str, int]:
        return {log_id: len(stamps) for log_id, stamps in self._stamps.items()}

    async def leaf_timestamp(self, log_id: str, index: int) -> datetime | None:
        stamps = self._stamps[log_id]
        if index < 0 or index >= len(stamps):
            return None
        return stamps[index]


_BASE = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def _stamps(count: int, gap_seconds: int = 60) -> list[datetime]:
    return [_BASE + timedelta(seconds=gap_seconds * i) for i in range(count)]


def test_locate_start_index_finds_hour_boundary() -> None:
    fetcher = _FakeTimestampFetcher({"log": _stamps(600)})  # 600 minutes of entries
    since = _BASE + timedelta(minutes=240)

    result = _run_search(fetcher, "log", since, 600)

    assert result == 240


def _run_search(fetcher, log_id: str, since: datetime, tree_size: int) -> int:
    import asyncio

    return asyncio.run(
        locate_start_index(fetcher, log_id=log_id, since=since, tree_size=tree_size)
    )


def test_locate_start_index_clamps_to_bounds() -> None:
    fetcher = _FakeTimestampFetcher({"log": _stamps(100)})

    before_all = _run_search(fetcher, "log", _BASE - timedelta(days=1), 100)
    after_all = _run_search(fetcher, "log", _BASE + timedelta(days=1), 100)

    assert before_all == 0
    assert after_all == 100


def test_locate_start_index_survives_unparseable_entries() -> None:
    stamps = _stamps(50)
    fetcher = _FakeTimestampFetcher({"log": stamps})

    class _HoleyFetcher(_FakeTimestampFetcher):
        async def leaf_timestamp(self, log_id: str, index: int) -> datetime | None:
            if index in (23, 24):
                return None  # simulate unparseable leaves
            return stamps[index]

    result = _run_search(_HoleyFetcher({"log": stamps}), "log", stamps[30], 50)

    assert result == 30


def test_start_config_rejects_bad_knobs() -> None:
    with pytest.raises(ValueError):
        StartConfig(hours=0)
    with pytest.raises(ValueError):
        StartConfig(hours=1, probe_concurrency=0)
    with pytest.raises(ValueError):
        StartConfig(hours=1, claim_limit=-1)


def test_run_follow_cancels_the_digest_loop_when_the_sweep_fails() -> None:
    """A failed sweep must not leave the digest loop running in the background.

    ``_run_follow`` awaits the ingest task first; if that raises, the digest
    task used to be neither awaited nor cancelled, so it kept spinning against
    a work queue that could never be refilled.
    """
    import asyncio

    from ai_tools_before_launch.ingest.runner import _run_follow

    class _Store:
        def __init__(self) -> None:
            self.cursors: dict[str, str] = {}

        def get_source_cursor(self, name: str) -> str | None:
            return self.cursors.get(name)

        def set_source_cursor(self, name: str, cursor: str) -> None:
            self.cursors[name] = cursor

        def pending_ct_discovery_work_count(self) -> int:
            return 1  # never drains, so the digest loop would spin forever

        def due_ct_discovery_work_count(self, *, now) -> int:  # noqa: ANN001
            return 1  # something is always claimable, so the loop keeps going

    class _Log:
        log_id = "log"

    class _Fetcher:
        logs = (_Log(),)

        async def tree_sizes(self, *, on_error=None) -> dict[str, int]:
            return {"log": 20}

        async def fetch_entries(self, *_args, **_kwargs):
            await asyncio.sleep(0.05)  # let the digest loop start first
            raise RuntimeError("sweep exploded")

    class _Poller:
        source_name = "ct_log"

    digest = {"rounds": 0, "cancelled": False}

    async def digest_once():
        digest["rounds"] += 1
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            digest["cancelled"] = True
            raise
        raise AssertionError("digest_once should have been cancelled")

    async def go() -> None:
        with pytest.raises(RuntimeError, match="sweep exploded"):
            await _run_follow(
                store=_Store(),
                fetcher=_Fetcher(),
                poller=_Poller(),
                digest_once=digest_once,
                config=StartConfig(follow=True, idle_seconds=0.0, entries=5),
                progress=lambda _event: None,
            )
        # Asserted here, not after asyncio.run(): teardown would cancel any
        # leftover task itself and hide the leak this test exists to catch.
        assert digest["rounds"] == 1, "the digest loop should have been entered"
        assert digest["cancelled"] is True, "the digest loop was left running"
        leftover = [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and not task.done()
        ]
        assert leftover == [], f"tasks outlived _run_follow: {leftover}"

    asyncio.run(go())


def test_filter_pipeline_parallel_rdap_matches_sequential() -> None:
    seen: list[str] = []
    observed_peak = {"concurrent": 0, "current": 0}

    def slow_fetcher(domain: str) -> Registration | None:
        import threading
        import time

        with threading.Lock():
            observed_peak["current"] += 1
            observed_peak["concurrent"] = max(
                observed_peak["concurrent"], observed_peak["current"]
            )
        time.sleep(0.02)
        seen.append(domain)
        with threading.Lock():
            observed_peak["current"] -= 1
        return Registration(
            domain=domain,
            registration_date=datetime.now(UTC) - timedelta(days=2),
            registrar="Test",
            statuses=(),
        )

    domains = [f"d{i}.com" for i in range(12)]
    pipeline = FilterPipeline(
        rdap_fetcher=slow_fetcher,
        dns_checker=lambda kept: {},
        rdap_concurrency=4,
        require_dns=False,
    )
    decisions = pipeline.evaluate(domains)

    assert [d.domain for d in decisions] == domains
    assert all(d.candidate is not None for d in decisions)
    assert len(seen) == 12
    assert observed_peak["concurrent"] <= 4
    assert observed_peak["concurrent"] >= 2  # actually parallel, not accidental serial


class _CursorStore:
    """Minimal store that keeps the cursor string in memory."""

    def __init__(self, cursor: str | None = None) -> None:
        self.cursor = cursor
        self.writes = 0

    def get_source_cursor(self, _name: str) -> str | None:
        return self.cursor

    def set_source_cursor(self, _name: str, cursor: str) -> None:
        self.cursor = cursor
        self.writes += 1


def test_merge_cursor_keeps_the_other_logs_entries() -> None:
    """The cursor is one JSON map shared by every log in a single row.

    Writing ``json.dumps({log_id: index})`` replaced the whole map, so the
    next cycle saw only the last log and reset every other log to the tree
    head — silently discarding everything they had already ingested.
    """
    import json

    from ai_tools_before_launch.ingest.runner import _merge_cursor

    store = _CursorStore(json.dumps({"alpha": 100, "beta": 200}))

    _merge_cursor(store, "ct_log", "beta", 500)

    assert json.loads(store.cursor or "{}") == {"alpha": 100, "beta": 500}


def test_merge_cursor_never_moves_backwards() -> None:
    import json

    from ai_tools_before_launch.ingest.runner import _merge_cursor

    store = _CursorStore(json.dumps({"alpha": 100, "beta": 200}))

    _merge_cursor(store, "ct_log", "alpha", 50)

    assert json.loads(store.cursor or "{}") == {"alpha": 100, "beta": 200}
    assert store.writes == 0, "a backwards cursor must not be written at all"


def test_merge_cursor_starts_from_an_empty_map() -> None:
    import json

    from ai_tools_before_launch.ingest.runner import _merge_cursor

    store = _CursorStore(None)

    _merge_cursor(store, "ct_log", "alpha", 7)

    assert json.loads(store.cursor or "{}") == {"alpha": 7}


def test_ingest_log_window_abandons_a_permanently_failing_page() -> None:
    """A page that never succeeds must be given up on, not retried forever.

    The old loop re-inserted a failed span at the front of the list with no
    attempt counter, so one broken range pinned a worker indefinitely and
    the sweep never finished. The window must instead abandon it, report
    it, and leave the cursor *before* the hole.
    """
    import asyncio

    from ai_tools_before_launch.ingest.runner import _ingest_log_window

    class _Poller:
        source_name = "ct_log"

        async def ingest_entries(self, entries):  # noqa: ANN001, ANN201
            return len(entries), 0

    class _Fetcher:
        def __init__(self) -> None:
            self.attempts: dict[tuple[int, int], int] = {}

        async def fetch_entries(self, _log_id: str, start: int, end: int):  # noqa: ANN201
            key = (start, end)
            self.attempts[key] = self.attempts.get(key, 0) + 1
            if start == 500:
                raise RuntimeError("this page is cursed")
            return [{"leaf_input": ""} for _ in range(end - start + 1)], None

    fetcher = _Fetcher()
    store = _CursorStore()

    async def go():  # noqa: ANN202
        return await _ingest_log_window(
            store=store,
            fetcher=fetcher,
            poller=_Poller(),
            log_id="log",
            start=0,
            tree_size=1200,
            config=StartConfig(
                page_max_attempts=3,
                page_delay_seconds=0.0,
                page_fetch_concurrency=2,
                queue_maxsize=4,
            ),
            progress=lambda _event: None,
        )

    result = asyncio.run(go())

    assert result.abandoned_spans == ((500, 999),)
    assert fetcher.attempts[(500, 999)] == 3, "the cursed page was retried forever"
    assert fetcher.attempts[(1000, 1199)] == 1
    assert result.contiguous == 500, "the cursor must stop before the hole"


def test_ingest_log_window_gives_up_on_an_empty_page() -> None:
    """An empty ``get-entries`` response must not loop on the same range.

    A server answering 200 with zero entries makes the "repair" sub-span
    identical to the span just requested, so the sweep spun on it forever.
    """
    import asyncio

    from ai_tools_before_launch.ingest.runner import _ingest_log_window

    class _Poller:
        source_name = "ct_log"

        async def ingest_entries(self, entries):  # noqa: ANN001, ANN201
            return len(entries), 0

    class _Fetcher:
        def __init__(self) -> None:
            self.calls = 0

        async def fetch_entries(self, _log_id: str, start: int, end: int):  # noqa: ANN201
            self.calls += 1
            if start == 500:
                return [], None  # 200 with an empty entry list
            return [{"leaf_input": ""} for _ in range(end - start + 1)], None

    fetcher = _Fetcher()

    async def go():  # noqa: ANN202
        return await _ingest_log_window(
            store=_CursorStore(),
            fetcher=fetcher,
            poller=_Poller(),
            log_id="log",
            start=0,
            tree_size=1200,
            config=StartConfig(
                page_max_attempts=2,
                page_delay_seconds=0.0,
                page_fetch_concurrency=1,
            ),
            progress=lambda _event: None,
        )

    result = asyncio.run(go())

    assert result.abandoned_spans == ((500, 999),)
    assert result.contiguous == 500


def test_run_follow_skips_an_unreachable_log_and_keeps_sweeping() -> None:
    """One dead log must not abort ``start`` for every other log."""
    import asyncio
    import json

    from ai_tools_before_launch.ingest.runner import _run_follow

    class _Stop(Exception):
        pass

    class _Log:
        def __init__(self, log_id: str) -> None:
            self.log_id = log_id

    class _Fetcher:
        logs = (_Log("alive"), _Log("dead"))

        async def tree_sizes(self, *, on_error=None):  # noqa: ANN001, ANN201
            if on_error is not None:
                on_error(self.logs[1], RuntimeError("connection refused"))
            return {"alive": 3}

        async def fetch_entries(self, _log_id: str, start: int, end: int):  # noqa: ANN201
            return [{"leaf_input": ""} for _ in range(end - start + 1)], None

    class _Store(_CursorStore):
        def pending_ct_discovery_work_count(self) -> int:
            return 0

        def due_ct_discovery_work_count(self, *, now) -> int:  # noqa: ANN001
            return 0

    class _Poller:
        source_name = "ct_log"

        async def ingest_entries(self, entries):  # noqa: ANN001, ANN201
            return len(entries), 0

    class _Summary:
        probes_run = 0
        candidates_created = 0

    events: list[dict] = []

    def progress(event: dict) -> None:
        events.append(event)
        if event["event"] == "start.cycle":
            raise _Stop

    async def digest_once():  # noqa: ANN202
        return _Summary()

    store = _Store()

    async def go() -> None:
        with pytest.raises(_Stop):
            await _run_follow(
                store=store,
                fetcher=_Fetcher(),
                poller=_Poller(),
                digest_once=digest_once,
                config=StartConfig(
                    entries=1, idle_seconds=0.0, round_delay_seconds=0.0
                ),
                progress=progress,
            )

    asyncio.run(go())

    unreachable = next(
        event for event in events if event["event"] == "start.logs_unreachable"
    )
    assert unreachable["logs"] == ["dead"]
    assert unreachable["reachable"] == ["alive"]
    assert unreachable["errors"] == {"dead": "RuntimeError: connection refused"}
    # The reachable log still swept all the way to its head, and the dead one
    # never got a cursor entry invented for it.
    assert events[-1]["event"] == "start.cycle"
    assert json.loads(store.cursor or "{}") == {"alive": 3}


def test_digest_loop_exits_when_nothing_is_due_even_if_work_is_pending() -> None:
    """A root parked on a future retry must not pin the digest loop forever.

    The exit condition used to be ``pending == 0``, which counts every
    unfinished root — including ones whose next attempt is hours away. One
    such root kept the loop spinning inside cycle 1, so the next sweep never
    started and the cursor never advanced again.
    """
    import asyncio
    import json

    from ai_tools_before_launch.ingest.runner import _run_follow

    class _Stop(Exception):
        pass

    class _Log:
        log_id = "alive"

    class _Fetcher:
        logs = (_Log(),)

        async def tree_sizes(self, *, on_error=None):  # noqa: ANN001, ANN201
            return {"alive": 1}

        async def fetch_entries(self, _log_id: str, start: int, end: int):  # noqa: ANN201
            return [{"leaf_input": ""} for _ in range(end - start + 1)], None

    class _Store(_CursorStore):
        def pending_ct_discovery_work_count(self) -> int:
            return 7  # a real backlog, but none of it claimable right now

        def due_ct_discovery_work_count(self, *, now) -> int:  # noqa: ANN001
            return 0

    class _Poller:
        source_name = "ct_log"

        async def ingest_entries(self, entries):  # noqa: ANN001, ANN201
            return len(entries), 0

    class _Summary:
        probes_run = 0
        candidates_created = 0

    events: list[dict] = []
    digest_rounds = 0
    store = _Store()

    def progress(event: dict) -> None:
        events.append(event)
        if event["event"] == "start.cycle":
            raise _Stop

    async def digest_once():  # noqa: ANN202
        nonlocal digest_rounds
        digest_rounds += 1
        return _Summary()

    async def go() -> None:
        with pytest.raises(_Stop):
            await _run_follow(
                store=store,
                fetcher=_Fetcher(),
                poller=_Poller(),
                digest_once=digest_once,
                config=StartConfig(
                    entries=1, idle_seconds=0.0, round_delay_seconds=0.0
                ),
                progress=progress,
            )

    asyncio.run(go())

    assert events[-1]["event"] == "start.cycle", "the cycle never finished"
    assert digest_rounds == 0, "a round ran even though nothing was due"
    assert json.loads(store.cursor or "{}") == {"alive": 1}


class _StampFetcher:
    """A log whose entry ages are known and whose reads are counted."""

    def __init__(self, size: int, *, tail_age_seconds: int = 30) -> None:
        self.size = size
        self.tail_age_seconds = tail_age_seconds
        self.reads: list[tuple[str, int]] = []
        self.fail = False

    @property
    def logs(self):  # noqa: ANN201 - duck-typed for the runner
        return (type("_Log", (), {"log_id": "log"})(),)

    async def leaf_timestamp(self, log_id: str, index: int):  # noqa: ANN201
        if self.fail:
            raise RuntimeError("log answered 503")
        self.reads.append((log_id, index))
        now = datetime.now(UTC)
        # Index 0 is oldest; each step is one minute, and the tail sits
        # `tail_age_seconds` behind the wall clock.
        return now - timedelta(seconds=self.tail_age_seconds + 60 * (self.size - 1 - index))

    async def tree_sizes(self) -> dict[str, int]:
        return {"log": self.size}


def test_measure_freshness_separates_our_debt_from_the_logs_own_lag() -> None:
    """Behind by 100 entries, but the log tail itself is 30s old.

    ``entries_behind`` is our own debt (actionable), ``processed_lag_seconds``
    is what the sighting really costs in seconds, and ``head_lag_seconds``
    isolates the log's publication delay — the floor nobody can beat.
    """
    import asyncio

    from ai_tools_before_launch.ingest.runner import measure_freshness

    fetcher = _StampFetcher(1000, tail_age_seconds=30)
    measured = asyncio.run(
        measure_freshness(fetcher, cursor={"log": 900}, tree_sizes={"log": 1000})
    )

    assert len(measured) == 1
    log = measured[0]
    assert log.entries_behind == 100
    assert log.cursor == 900
    # tail is 30s old; index 899 is 100 entries earlier = 6000s more
    assert log.processed_lag_seconds == pytest.approx(6030, abs=5)
    assert log.head_lag_seconds == pytest.approx(30, abs=5)


def test_measure_freshness_reads_the_tail_once_when_caught_up() -> None:
    """Caught up means the tail read answers both questions: one request."""
    import asyncio

    from ai_tools_before_launch.ingest.runner import measure_freshness

    fetcher = _StampFetcher(1000, tail_age_seconds=12)
    measured = asyncio.run(
        measure_freshness(fetcher, cursor={"log": 1000}, tree_sizes={"log": 1000})
    )

    log = measured[0]
    assert log.entries_behind == 0
    assert log.processed_lag_seconds == pytest.approx(12, abs=5)
    assert fetcher.reads == [("log", 999)], "a caught-up log must cost one read"


def test_measure_freshness_survives_an_unreadable_log() -> None:
    """Measuring must never fail a cycle that already ingested successfully."""
    import asyncio

    from ai_tools_before_launch.ingest.runner import measure_freshness

    fetcher = _StampFetcher(10)
    fetcher.fail = True
    measured = asyncio.run(
        measure_freshness(fetcher, cursor={"log": 5}, tree_sizes={"log": 10})
    )

    log = measured[0]
    assert log.entries_behind == 5
    assert log.processed_lag_seconds is None and log.head_lag_seconds is None


def test_freshness_report_warns_only_when_we_are_the_bottleneck() -> None:
    """A stale log tail is the floor of the approach, not a fault of ours."""
    from ai_tools_before_launch.ingest.runner import LogFreshness, _freshness_report

    def entry(behind: int, processed: float, head: float) -> LogFreshness:
        return LogFreshness(
            log_id="log",
            tree_size=1000,
            cursor=1000 - behind,
            entries_behind=behind,
            processed_lag_seconds=processed,
            head_lag_seconds=head,
        )

    payload, warning = _freshness_report(
        (entry(0, 900.0, 900.0),), lag_warn_seconds=120.0
    )
    assert payload["processed_lag_seconds"] == 900.0
    assert warning is None, "a slow log must not be reported as our own lag"

    payload, warning = _freshness_report(
        (entry(250, 640.0, 30.0),), lag_warn_seconds=120.0
    )
    assert payload["entries_behind"] == 250
    assert warning is not None and warning["event"] == "start.lagging"

    _, warning = _freshness_report((entry(250, 640.0, 30.0),), lag_warn_seconds=900.0)
    assert warning is None, "a threshold above the observed lag must stay quiet"


def test_start_config_rejects_a_negative_lag_threshold() -> None:
    with pytest.raises(ValueError):
        StartConfig(lag_warn_seconds=-1)


def test_name_score_threshold_gates_before_rdap_but_keeps_input_order() -> None:
    """S1 is free and S2 costs a rate-limited request, so it runs first.

    The rejection must still be reported as a decision for its own domain, in
    the caller's order: the orchestrator settles exactly the items it claimed.
    """
    from ai_tools_before_launch.filter.pipeline import FilterPipeline

    asked: list[str] = []

    def rdap(domain: str):  # noqa: ANN202
        asked.append(domain)
        return Registration(
            domain=domain,
            registration_date=datetime.now(UTC) - timedelta(days=3),
            registrar="Test",
            statuses=(),
        )

    domains = ["xqztvbk.xyz", "openmind.tech", "moralespsychservices.com"]
    pipeline = FilterPipeline(
        rdap_fetcher=rdap,
        dns_checker=lambda kept: {},
        min_name_score=0.3,
    )
    decisions = pipeline.evaluate(domains)

    assert [d.domain for d in decisions] == domains, "input order must survive"
    rejected = [d for d in decisions if d.reason == "name_score_low"]
    assert [d.domain for d in rejected] == ["xqztvbk.xyz"]
    assert rejected[0].retryable is False, "a name score never improves by retrying"
    assert "xqztvbk.xyz" not in asked, "a gated domain must not spend RDAP quota"
    assert {"openmind.tech", "moralespsychservices.com"} <= set(asked)


def test_name_score_threshold_of_zero_keeps_everything() -> None:
    """Recall is the default: the tool exists to find things, not to filter."""
    from ai_tools_before_launch.filter.pipeline import FilterPipeline

    domains = ["xqztvbk.xyz", "a1b2c3d4.tech"]
    only_counts = {"rdap": 0}

    def rdap(domain: str):  # noqa: ANN202
        only_counts["rdap"] += 1
        return Registration(
            domain=domain,
            registration_date=datetime.now(UTC) - timedelta(days=3),
            registrar="Test",
            statuses=(),
        )

    decisions = FilterPipeline(rdap_fetcher=rdap, dns_checker=lambda kept: {}).evaluate(
        domains
    )

    assert [d.domain for d in decisions] == domains
    assert all(d.candidate is not None for d in decisions)
    assert only_counts["rdap"] == 2


def test_filter_pipeline_rejects_an_out_of_range_name_score() -> None:
    from ai_tools_before_launch.filter.pipeline import FilterPipeline

    with pytest.raises(ValueError):
        FilterPipeline(min_name_score=1.5)


class _CycleStore:
    """Cursor + counters, enough for one caught-up cycle."""

    def __init__(self, cursor: str) -> None:
        self.cursor = cursor

    def get_source_cursor(self, _name: str) -> str:
        return self.cursor

    def set_source_cursor(self, _name: str, cursor: str) -> None:
        self.cursor = cursor

    def pending_ct_discovery_work_count(self) -> int:
        return 0

    def due_ct_discovery_work_count(self, *, now) -> int:  # noqa: ANN001
        return 0


def test_run_follow_reports_freshness_on_every_cycle() -> None:
    """The measurement has to reach the caller, not just the unit tests.

    A caught-up log whose tail is 500 seconds old is *not* our lag, so the
    cycle reports it and stays quiet — the exact opposite of the old
    behaviour, where nothing was reported at all.
    """
    import asyncio
    import json

    from ai_tools_before_launch.ingest.runner import _run_follow

    class _Stop(Exception):
        pass

    class _Log:
        log_id = "log"

    class _Fetcher:
        logs = (_Log(),)

        async def tree_sizes(self, *, on_error=None) -> dict[str, int]:
            return {"log": 100}

        async def leaf_timestamp(self, _log_id: str, _index: int):  # noqa: ANN201
            return datetime.now(UTC) - timedelta(seconds=500)

    class _Poller:
        source_name = "ct_log"

    events: list[dict] = []

    def progress(event: dict) -> None:
        events.append(event)
        if event["event"] == "start.cycle":
            raise _Stop

    async def go() -> None:
        with pytest.raises(_Stop):
            await _run_follow(
                store=_CycleStore(json.dumps({"log": 100})),
                fetcher=_Fetcher(),
                poller=_Poller(),
                digest_once=lambda: None,
                config=StartConfig(entries=1, idle_seconds=0.0),
                progress=progress,
            )

    asyncio.run(go())

    cycle = events[-1]
    assert cycle["event"] == "start.cycle"
    freshness = cycle["freshness"]
    assert freshness["entries_behind"] == 0
    assert freshness["processed_lag_seconds"] == pytest.approx(500, abs=5)
    assert freshness["logs"][0]["log"] == "log"
    assert [e["event"] for e in events].count("start.lagging") == 0
