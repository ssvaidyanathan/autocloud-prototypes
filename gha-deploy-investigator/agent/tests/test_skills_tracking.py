"""Unit tests for ADK skill tracking and tool callbacks."""

from unittest.mock import MagicMock

from agent.app.contracts.findings import Findings
from agent.app.specialists.base import DiagnosticResult, SkillTracker


def test_after_tool_callback_captures_loaded_skills() -> None:
    """Verify after_tool_callback captures loaded skill names and deduplicates."""
    tracker = SkillTracker()

    tool_load = MagicMock()
    tool_load.name = "load_skill"

    tool_other = MagicMock()
    tool_other.name = "fetch_job_logs"

    # Turn 1: load_skill with 'lint-troubleshooting'
    res = tracker.after_tool_callback(
        tool_load,
        tool_args={"skill_name": "lint-troubleshooting"},
        tool_context=None,
        tool_output=None,
    )
    assert res is None
    assert tracker.skills_used == ["lint-troubleshooting"]

    # Turn 2: regular tool call (should not add to skills_used)
    tracker.after_tool_callback(
        tool_other,
        tool_args={"job_id": 123},
        tool_context=None,
        tool_output="logs...",
    )
    assert tracker.skills_used == ["lint-troubleshooting"]
    assert len(tracker.queries_run) == 1
    assert "fetch_job_logs" in tracker.queries_run[0]

    # Turn 3: load_skill with 'container-build-troubleshooting'
    tracker.after_tool_callback(
        tool_load,
        tool_args={"skill_name": "container-build-troubleshooting"},
        tool_context=None,
        tool_output=None,
    )
    assert tracker.skills_used == [
        "lint-troubleshooting",
        "container-build-troubleshooting",
    ]

    # Turn 4: load_skill duplicate (deduplication check)
    tracker.after_tool_callback(
        tool_load,
        tool_args={"skill_name": "lint-troubleshooting"},
        tool_context=None,
        tool_output=None,
    )
    assert tracker.skills_used == [
        "lint-troubleshooting",
        "container-build-troubleshooting",
    ]


def test_skill_first_tracking_turn_one_load_skill() -> None:
    """Verify skill_first is True when Turn 1 is load_skill."""
    tracker = SkillTracker()

    tool_load = MagicMock()
    tool_load.name = "load_skill"
    tool_query = MagicMock()
    tool_query.name = "fetch_job_logs"

    # Turn 1: load_skill
    tracker.after_tool_callback(
        tool_load, tool_args={"skill_name": "cloudrun-deploy-troubleshooting"}
    )
    assert tracker.skill_first is True

    # Turn 2: query
    tracker.after_tool_callback(tool_query, tool_args={"job_id": 456})
    assert tracker.skill_first is True

    # Populate Findings
    f = Findings(summary="Cloud Run deploy failure", confidence=0.85)
    populated = tracker.populate_findings(f)
    assert populated.skill_first is True
    assert populated.skills_used == ["cloudrun-deploy-troubleshooting"]


def test_skill_first_tracking_turn_one_not_load_skill() -> None:
    """Verify skill_first is False when Turn 1 is NOT load_skill."""
    tracker = SkillTracker()

    tool_load = MagicMock()
    tool_load.name = "load_skill"
    tool_query = MagicMock()
    tool_query.name = "fetch_job_logs"

    # Turn 1: regular query
    tracker.after_tool_callback(tool_query, tool_args={"job_id": 456})
    assert tracker.skill_first is False

    # Turn 2: load_skill
    tracker.after_tool_callback(
        tool_load, tool_args={"skill_name": "cloudrun-deploy-troubleshooting"}
    )
    assert tracker.skill_first is False
    assert tracker.skills_used == ["cloudrun-deploy-troubleshooting"]


def test_max_tool_calls_budget_enforcement() -> None:
    """Verify before_tool_callback enforces max_tool_calls budget limit."""
    tracker = SkillTracker(max_tool_calls=2)

    tool = MagicMock()
    tool.name = "fetch_job_logs"

    # Call 1
    assert tracker.before_tool_callback(tool, tool_args={"job_id": 1}) is None
    tracker.after_tool_callback(tool, tool_args={"job_id": 1})

    # Call 2
    assert tracker.before_tool_callback(tool, tool_args={"job_id": 2}) is None
    tracker.after_tool_callback(tool, tool_args={"job_id": 2})

    # Call 3 (exceeds budget)
    res = tracker.before_tool_callback(tool, tool_args={"job_id": 3})
    assert res is not None
    assert res.get("status") == "budget_reached"


def test_populate_diagnostic_result() -> None:
    """Verify populate_diagnostic_result copies skills_used, queries_run, and skill_first."""
    tracker = SkillTracker()

    tool_load = MagicMock()
    tool_load.name = "load_skill"
    tracker.after_tool_callback(tool_load, tool_args={"skill_name": "lint-troubleshooting"})

    tool_query = MagicMock()
    tool_query.name = "fetch_job_logs"
    tracker.after_tool_callback(tool_query, tool_args={"job_id": 999})

    diag = DiagnosticResult(
        stage="lint",
        specialist_name="pipeline_logs_specialist",
        summary="Lint failed",
        probable_cause="Unused import",
        confidence=0.9,
    )

    populated = tracker.populate_diagnostic_result(diag)
    assert populated.skill_first is True
    assert populated.skills_used == ["lint-troubleshooting"]
    assert len(populated.queries_run) == 1
