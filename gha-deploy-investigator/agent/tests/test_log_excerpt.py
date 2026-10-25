"""Regression tests for decisive, error-bearing log excerpt selection."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from agent.app.contracts.incident import Incident
from agent.app.specialists.pipeline_logs import (
    PipelineLogsSpecialist,
    extract_decisive_log_excerpt,
    strip_runner_cleanup,
)

FIXTURE = Path(__file__).parent / "fixtures" / "build_failure_runner_cleanup_log.txt"
DECISIVE_BUILD_ERROR = (
    'ERROR: failed to solve: failed to compute cache key: failed to calculate checksum of ref: '
    '"/config/cache-warmup.json": not found'
)
CLEANUP_FRAGMENTS = (
    "Post job cleanup",
    "Removing credentials config",
    "Cleaning up orphan processes",
    "--unset-all",
    "Finishing:",
)


def _ts(i: int) -> str:
    return f"2026-10-25T00:44:{i % 60:02d}.{i:07d}Z "


def _cleanup_noise(min_chars: int = 2000) -> str:
    """Realistic runner post-step cleanup block of at least ``min_chars``."""
    block = [
        "##[group]Post job cleanup.",
        "[command]/usr/bin/git version",
        "git version 2.43.0",
        "Temporarily overriding HOME='/home/runner/work/_temp/abc' before making global "
        "git config changes",
        "Adding repository directory to the temporary git global config as a safe directory",
        "[command]/usr/bin/git config --global --add safe.directory /home/runner/work/repo/repo",
        "[command]/usr/bin/git config --local --name-only --get-regexp core\\.sshCommand",
        "[command]/usr/bin/git config --local --name-only --get-regexp "
        "http\\.https\\:\\/\\/github\\.com\\/\\.extraheader",
        "http.https://github.com/.extraheader",
        "[command]/usr/bin/git config --local --unset-all http.https://github.com/.extraheader",
        "Removing credentials config...",
        "##[endgroup]",
    ]
    out: list[str] = []
    i = 0
    while sum(len(x) for x in out) < min_chars:
        out.append(_ts(i) + block[i % len(block)] + "\n")
        i += 1
    out.append(_ts(i) + "Cleaning up orphan processes\n")
    out.append(_ts(i + 1) + "Finishing: Build Container Image\n")
    return "".join(out)


def _make_incident(stage: str) -> Incident:
    return Incident(
        id="gha-test-1",
        source="github",
        external_id="1",
        external_key="my-org/sample-app#1",
        external_url="https://github.com/my-org/sample-app/actions/runs/1",
        title="[CI/CD Failure] sample-app: CI",
        description="Run failed",
        severity="P2",
        service_hint="sample-app",
        opened_at=datetime.now(timezone.utc),
        github_repo="my-org/sample-app",
        github_run_id=1,
        github_head_sha="e4b2a8d39c01",
        github_branch="main",
        github_failing_stage=stage,
    )


def _runner_log_excerpt(res: Any) -> str:
    items = [e for e in res.evidence_items if e.kind == "github_runner_log"]
    assert len(items) == 1
    return items[0].excerpt


@pytest.fixture
def real_build_log() -> str:
    return FIXTURE.read_text()


class TestRealBuildFailureFixture:
    def test_fixture_excerpt_keeps_decisive_error_and_drops_cleanup(
        self, real_build_log: str
    ) -> None:
        excerpt = extract_decisive_log_excerpt(real_build_log, max_chars=1500)
        assert DECISIVE_BUILD_ERROR in excerpt
        assert (
            '#5 ERROR: failed to calculate checksum of ref: "/config/cache-warmup.json"'
            in excerpt
        )
        assert "COPY config/cache-warmup.json" in excerpt
        for fragment in CLEANUP_FRAGMENTS:
            assert fragment not in excerpt
        assert len(excerpt) <= 1500

    def test_fixture_with_long_cleanup_tail_where_blind_tail_loses_error(
        self, real_build_log: str
    ) -> None:
        log_text = real_build_log + _cleanup_noise(2000)
        # Precondition: the old blind tail misses the decisive error entirely.
        assert "failed to solve" not in log_text[-1500:]

        excerpt = extract_decisive_log_excerpt(log_text, max_chars=1500)
        assert DECISIVE_BUILD_ERROR in excerpt
        for fragment in CLEANUP_FRAGMENTS + ("git version", "safe.directory", "extraheader"):
            assert fragment not in excerpt
        assert len(excerpt) <= 1500

    def test_timestamps_are_removed_from_excerpt(self, real_build_log: str) -> None:
        excerpt = extract_decisive_log_excerpt(real_build_log)
        assert "2026-10-25T00:43:55" not in excerpt

    def test_tight_budget_prioritises_decisive_error_lines(self, real_build_log: str) -> None:
        excerpt = extract_decisive_log_excerpt(real_build_log + _cleanup_noise(), max_chars=300)
        assert DECISIVE_BUILD_ERROR in excerpt
        assert "#5 ERROR: failed to calculate checksum" in excerpt
        assert len(excerpt) <= 300


class TestErrorMarkerWindowing:
    @staticmethod
    def _long_log(error_line: str, before: int = 200, after: int = 200) -> str:
        lines = [f"{_ts(i)}step output line {i} doing routine work" for i in range(before)]
        lines.append(f"{_ts(before)}Running: make package")
        lines.append(f"{_ts(before + 1)}{error_line}")
        lines.append(f"{_ts(before + 2)}  details: see trace below")
        lines.extend(
            f"{_ts(i)}more trailing output line {i} after the failure"
            for i in range(before + 3, before + 3 + after)
        )
        return "\n".join(lines) + "\n"

    @pytest.mark.parametrize(
        "error_line",
        [
            "fatal: unable to access remote repository",
            "npm ERR! code ELIFECYCLE",
            "RuntimeError: unexpected state",
            "[ERROR] Failed to execute goal compile",
            "error [E0425]: cannot find value `x` in this scope",
            "FAILED: build.ninja target out/app",
        ],
    )
    def test_mid_log_error_is_windowed_with_context(self, error_line: str) -> None:
        excerpt = extract_decisive_log_excerpt(self._long_log(error_line), max_chars=800)
        assert error_line in excerpt
        assert "Running: make package" in excerpt
        assert "details: see trace below" in excerpt
        assert len(excerpt) <= 800
        # Window, not blind tail: the very last log line is far from the error.
        assert "more trailing output line 402" not in excerpt

    def test_window_respects_max_chars_with_many_markers(self) -> None:
        lines = [f"{_ts(i)}error: problem number {i} " + "x" * 80 for i in range(100)]
        excerpt = extract_decisive_log_excerpt("\n".join(lines), max_chars=500)
        assert len(excerpt) <= 500
        assert "error: problem number 99 " in excerpt
        assert "error: problem number 0 " in excerpt

    def test_generic_exit_line_is_not_preferred_over_cause(self) -> None:
        lines = (
            ["routine output"] * 300
            + ["Error: cannot open configuration file settings.toml"]
            + ["cleanup of temp dir"] * 300
            + ["##[error]Process completed with exit code 1."]
        )
        excerpt = extract_decisive_log_excerpt("\n".join(lines), max_chars=400)
        assert "Error: cannot open configuration file settings.toml" in excerpt
        assert len(excerpt) <= 400

    def test_single_oversized_error_line_is_clipped(self) -> None:
        excerpt = extract_decisive_log_excerpt("error: " + "y" * 5000, max_chars=200)
        assert excerpt.startswith("error: ")
        assert len(excerpt) <= 200


class TestRunnerCleanupStripping:
    def test_cleanup_stripped_without_error_markers(self) -> None:
        log_text = (
            f"{_ts(0)}Compiling module a\n{_ts(1)}Compiling module b\n{_ts(2)}Done in 3s\n"
            + _cleanup_noise(500)
        )
        excerpt = extract_decisive_log_excerpt(log_text)
        assert excerpt.endswith("Done in 3s")
        for fragment in CLEANUP_FRAGMENTS:
            assert fragment not in excerpt

    def test_strip_stops_at_real_output(self) -> None:
        log_text = "\n".join(
            [
                "real output 1",
                "real output 2",
                "Removing credentials config...",
                "git config --global --unset-all core.sshCommand",
                "Cleaning up orphan processes",
                "Finishing: Job",
            ]
        )
        assert strip_runner_cleanup(log_text) == ["real output 1", "real output 2"]

    def test_post_step_error_after_cleanup_header_is_preserved(self) -> None:
        log_text = "\n".join(
            ["step ok", "Post job cleanup.", "Error: post step upload failed", "Finishing: Job"]
        )
        excerpt = extract_decisive_log_excerpt(log_text)
        assert "Error: post step upload failed" in excerpt


class TestFallback:
    def test_short_log_without_markers_is_retained(self) -> None:
        log_text = "line one\nline two\nline three"
        assert extract_decisive_log_excerpt(log_text) == log_text

    def test_long_log_without_markers_keeps_tail(self) -> None:
        lines = [f"output line {i}" for i in range(500)]
        excerpt = extract_decisive_log_excerpt("\n".join(lines), max_chars=300)
        assert excerpt.endswith("output line 499")
        assert "output line 0\n" not in excerpt
        assert len(excerpt) <= 300

    def test_empty_log(self) -> None:
        assert extract_decisive_log_excerpt("") == ""


class TestSpecialistIntegration:
    @pytest.fixture(autouse=True)
    def _no_diff(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "agent.app.specialists.pipeline_logs.get_commit_diff", lambda **_: {"files": []}
        )

    def test_diagnose_build_uses_decisive_excerpt(self, real_build_log: str) -> None:
        log_text = real_build_log + _cleanup_noise(2000)
        res = PipelineLogsSpecialist()._diagnose_build(
            _make_incident("build"), {"id": 1}, {"name": "Build Container Image"}, log_text
        )
        excerpt = _runner_log_excerpt(res)
        assert DECISIVE_BUILD_ERROR in excerpt
        assert "Cleaning up orphan processes" not in excerpt
        assert "--unset-all" not in excerpt

    def test_diagnose_test_uses_decisive_excerpt(self) -> None:
        log_text = (
            "\n".join(f"tests/test_mod.py::test_case_{i} PASSED" for i in range(150))
            + "\nE   AssertionError: assert 7 == 6\n"
            + "FAILED tests/test_calc.py::test_total - AssertionError: assert 7 == 6\n"
            + "##[error]Process completed with exit code 1.\n"
            + _cleanup_noise(2000)
        )
        res = PipelineLogsSpecialist()._diagnose_test(
            _make_incident("test"), {"id": 1}, {"name": "Run Tests"}, log_text
        )
        excerpt = _runner_log_excerpt(res)
        assert "FAILED tests/test_calc.py::test_total - AssertionError: assert 7 == 6" in excerpt
        assert "Cleaning up orphan processes" not in excerpt
        assert "--unset-all" not in excerpt

    def test_diagnose_generic_uses_decisive_excerpt(self) -> None:
        log_text = (
            "\n".join(f"processing record {i}" for i in range(100))
            + "\nInvoking migrate.sh\nfatal: database schema version mismatch\n"
            + "##[error]Process completed with exit code 2.\n"
            + _cleanup_noise(2000)
        )
        res = PipelineLogsSpecialist()._diagnose_generic(
            _make_incident("pipeline"), {"id": 1}, {"name": "Migrate"}, log_text
        )
        excerpt = _runner_log_excerpt(res)
        assert excerpt.startswith("Step 'Migrate' failed:")
        assert "fatal: database schema version mismatch" in excerpt
        assert "Invoking migrate.sh" in excerpt
        assert "Cleaning up orphan processes" not in excerpt
        assert "--unset-all" not in excerpt
