"""Tests for the idempotency guard (has_recent_success / should_skip).

We mock ``session_scope`` to avoid touching a real database.
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest

from app.governance.lineage import has_recent_success, should_skip


def _fake_session_returning(row):
    """Helper: build a session_scope() context whose execute() returns ``row``."""
    session = MagicMock()
    session.execute.return_value.first.return_value = row
    ctx = MagicMock()
    ctx.__enter__.return_value = session
    ctx.__exit__.return_value = False
    return ctx


def test_has_recent_success_returns_uuid_when_db_has_match():
    run_uuid = uuid.uuid4()
    with patch(
        "app.governance.lineage.session_scope",
        return_value=_fake_session_returning((run_uuid,)),
    ):
        result = has_recent_success("gee.flood", ttl_hours=24)
    assert result == run_uuid


def test_has_recent_success_returns_none_when_db_returns_nothing():
    with patch(
        "app.governance.lineage.session_scope",
        return_value=_fake_session_returning(None),
    ):
        assert has_recent_success("gee.flood", ttl_hours=24) is None


def test_has_recent_success_accepts_string_uuid_from_psycopg():
    """psycopg sometimes returns UUIDs as strings depending on adapter setup."""
    run_uuid = uuid.uuid4()
    with patch(
        "app.governance.lineage.session_scope",
        return_value=_fake_session_returning((str(run_uuid),)),
    ):
        result = has_recent_success("gee.flood", ttl_hours=24)
    assert result == run_uuid


def test_should_skip_returns_none_when_fresh_is_true():
    """fresh=True must short-circuit before any DB call."""
    with patch("app.governance.lineage.session_scope") as mock_scope:
        assert should_skip("gee.flood", fresh=True) is None
        mock_scope.assert_not_called()


def test_should_skip_uses_pipeline_ttl_when_no_override():
    """Default TTL is read from configs/pipeline.yml::ingestion.skip_ttl_hours."""
    run_uuid = uuid.uuid4()
    with patch(
        "app.governance.lineage.load_pipeline_config",
        return_value={"ingestion": {"skip_ttl_hours": 12}},
    ), patch(
        "app.governance.lineage.session_scope",
        return_value=_fake_session_returning((run_uuid,)),
    ):
        result = should_skip("gee.flood", fresh=False)
    assert result == run_uuid


def test_should_skip_honors_explicit_ttl_override():
    run_uuid = uuid.uuid4()
    with patch(
        "app.governance.lineage.load_pipeline_config"
    ) as mock_cfg, patch(
        "app.governance.lineage.session_scope",
        return_value=_fake_session_returning((run_uuid,)),
    ):
        result = should_skip("gee.flood", fresh=False, ttl_hours=1)
    assert result == run_uuid
    # When an explicit ttl is passed, config should not be consulted.
    mock_cfg.assert_not_called()


@pytest.mark.parametrize("fresh_value", [True, False])
def test_should_skip_fresh_overrides_config(fresh_value):
    """Sanity matrix: fresh=True always skips DB; fresh=False always hits it."""
    with patch("app.governance.lineage.session_scope") as mock_scope:
        mock_scope.return_value = _fake_session_returning(None)
        with patch(
            "app.governance.lineage.load_pipeline_config",
            return_value={"ingestion": {"skip_ttl_hours": 24}},
        ):
            result = should_skip("any.source", fresh=fresh_value)
        if fresh_value:
            assert result is None
            mock_scope.assert_not_called()
        else:
            mock_scope.assert_called_once()
