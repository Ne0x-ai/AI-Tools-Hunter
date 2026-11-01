import json
from datetime import UTC, datetime
from typing import Self

import pytest

from ai_tools_before_launch import cli
from ai_tools_before_launch.api import create_app
from ai_tools_before_launch.domain.candidates import (
    CandidateOutcome,
    CandidateVersionDraft,
    Evidence,
    EvidenceType,
)
from ai_tools_before_launch.domain.reviews import ReviewAction, build_review_decision
from ai_tools_before_launch.ingest.ct_poller import CTCertificate, CTPage
from ai_tools_before_launch.storage.sqlite import SQLiteStore


def test_cli_serves_the_review_api_from_an_explicit_database(tmp_path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeUvicorn:
        @staticmethod
        def run(app, *, host: str, port: int) -> None:
            captured.update(app=app, host=host, port=port)

    monkeypatch.setattr(cli, "uvicorn", FakeUvicorn, raising=False)

    assert (
        cli.main(
            [
                "serve",
                "--database",
                str(tmp_path / "ai_tools_before_launch.db"),
                "--host",
                "127.0.0.1",
                "--port",
                "8765",
            ]
        )
        == 0
    )

    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8765
