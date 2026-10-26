"""Regression tests for decisive, error-bearing log excerpt selection."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from agent.app.contracts.incident import Incident
from agent.app.specialists.pipeline_logs import (
    ERROR_MARKER_PATTERN,
    EXCERPT_GAP_MARKER,
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
            "error[E0425]: cannot find value `x` in this scope",
            "FAILED: build.ninja target out/app",
            "src/a.ts(1,7): error TS2322: Type 'string' is not assignable to type 'number'.",
            "Program.cs(3,5): error CS0103: The name 'foo' does not exist",
            "make: *** [Makefile:3: all] Error 1",
            "--- FAIL: TestCharge (0.00s)",
            "FAIL: test_x (tests.test_mod.T)",
            "BUILD FAILED",
            "FAILED tests/a.py::test_total",
            "bash: line 1: foo: command not found",
        ],
    )
    def test_mid_log_error_is_windowed_with_context(self, error_line: str) -> None:
        log_text = self._long_log(error_line)
        # Window, not blind tail: the error is far from the end of the log.
        assert error_line not in log_text[-800:]
        excerpt = extract_decisive_log_excerpt(log_text, max_chars=800)
        assert error_line in excerpt
        assert "Running: make package" in excerpt
        assert "details: see trace below" in excerpt
        assert len(excerpt) <= 800

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

    @pytest.mark.parametrize(
        "line",
        [
            "Tests: 10 passed, failed: 0",
            "Ran 12 tests, 0 failed",
            "all checks passed",
            "Summary: 12 PASSED, 0 FAILED",
            "Summary: 12 PASSED, 0  FAILED",
            "FAILED: 0",
        ],
    )
    def test_zero_failure_summaries_are_not_markers(self, line: str) -> None:
        assert not ERROR_MARKER_PATTERN.search(line)

    @pytest.mark.parametrize("line", ["Summary: 11 PASSED, 1 FAILED", "10 FAILED", "FAILED: 3"])
    def test_nonzero_failure_counts_are_markers(self, line: str) -> None:
        assert ERROR_MARKER_PATTERN.search(line)

    def test_zero_failure_summary_does_not_displace_real_cause(self) -> None:
        lines = (
            ["routine output"] * 200
            + ["error: linker command failed with exit code 1"]
            + ["unrelated output"] * 200
            + ["Tests: 10 passed, failed: 0"]
        )
        excerpt = extract_decisive_log_excerpt("\n".join(lines), max_chars=400)
        assert "error: linker command failed with exit code 1" in excerpt
        assert len(excerpt) <= 400

    def test_rustc_cause_above_summary_error_is_kept(self) -> None:
        lines = [f"   Compiling dep{i} v0.1.{i}" for i in range(80)]
        lines += [
            "   Compiling app v0.1.0 (/home/runner/work/app/app)",
            "error[E0425]: cannot find value `totl` in this scope",
            " --> src/main.rs:4:20",
            "  |",
            '4 |     println!("{}", totl);',
            "  |                    ^^^^ help: a local variable with a similar name exists",
            "",
            "For more information about this error, try `rustc --explain E0425`.",
            'error: could not compile `app` (bin "app") due to 1 previous error',
            "##[error]Process completed with exit code 101.",
        ]
        log_text = "\n".join(lines)
        excerpt = extract_decisive_log_excerpt(log_text, max_chars=1500)
        assert "error[E0425]: cannot find value `totl` in this scope" in excerpt
        assert "src/main.rs:4:20" in excerpt
        assert "error: could not compile `app`" in excerpt
        assert len(excerpt) <= 1500

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


class TestTailCauseRetention:
    """R1 regressions: causes near the end of the log are retained in the excerpt.

    Covers causes the old blind tail carried as well as unrecognised causes in the
    lead-up to a terminal error that an earlier false-positive marker must not starve.
    """

    @staticmethod
    def _assert_tail_cause_kept(log_text: str, cause_lines: list[str], max_chars: int) -> None:
        for cause in cause_lines:
            # Precondition: the old blind tail carried the cause.
            assert cause in log_text[-max_chars:]
        excerpt = extract_decisive_log_excerpt(log_text, max_chars=max_chars)
        for cause in cause_lines:
            assert cause in excerpt
        assert len(excerpt) <= max_chars

    def test_unrecognised_cause_with_only_generic_exit_line_keeps_tail(self) -> None:
        cause_lines = [
            "Deploy target prod-eu rejected the artifact signature",
            "  signature digest sha256:abc123 does not match the trusted key set",
        ]
        lines = [f"uploading chunk {i} of artifact" for i in range(300)]
        lines += cause_lines
        lines += ["##[error]Process completed with exit code 3."]
        log_text = "\n".join(lines) + "\n" + _cleanup_noise(500)
        self._assert_tail_cause_kept("\n".join(lines), cause_lines, 1500)
        excerpt = extract_decisive_log_excerpt(log_text, max_chars=1500)
        for cause in cause_lines:
            assert cause in excerpt
        assert "##[error]Process completed with exit code 3." in excerpt
        assert len(excerpt) > 1000  # budget is spent, not shrunk to a few lines

    @pytest.mark.parametrize(
        "trailing_lines",
        [
            pytest.param(0, id="cause-at-end"),
            pytest.param(15, id="cause-15-lines-before-end"),
            pytest.param(60, id="cause-60-lines-before-end"),
        ],
    )
    @pytest.mark.parametrize(
        "marker_index",
        [
            pytest.param(0, id="marker-top"),
            pytest.param(150, id="marker-middle"),
            pytest.param(-40, id="marker-len-40"),
            pytest.param(-30, id="marker-len-30"),
        ],
    )
    def test_early_false_positive_marker_keeps_real_cause_near_tail(
        self, marker_index: int, trailing_lines: int
    ) -> None:
        cause_lines = [
            "Charge processor returned unexpected status 503",
            "  while handling request id=42 for merchant=acme",
        ]
        lines = [f"processing batch {i} of transactions" for i in range(300)]
        lines += cause_lines
        lines += [f"cleanup step {i} ok" for i in range(trailing_lines)]
        lines += ["##[error]Process completed with exit code 1."]
        position = marker_index if marker_index >= 0 else len(lines) + marker_index
        lines[position:position] = ["config: retry-on-error: true", "config: timeout: 30s"]
        self._assert_tail_cause_kept("\n".join(lines), cause_lines, 1500)

    def test_non_terminal_marker_keeps_cause_at_core_boundary(self) -> None:
        """Narrowed guarantee: a cause within ``max_chars - core_chars`` is kept."""
        max_chars = 1500
        cause = "Charge processor returned unexpected status 503"
        exit_line = "##[error]Process completed with exit code 1."
        head = [f"processing batch {i} of transactions" for i in range(300)]
        head[150:150] = ["config: retry-on-error: true", "config: timeout: 30s"]
        # Core: the marker plus one line either side; no command header present.
        core_text = "\n".join(head[149:152])
        core_chars = len(core_text) + len("\n" + EXCERPT_GAP_MARKER + "\n")
        budget = max_chars - core_chars

        def build(k: int) -> str:
            trailing = [f"cleanup step {i} ok" for i in range(k)]
            return "\n".join([*head, cause, *trailing, exit_line])

        k = 0
        while cause in build(k + 1)[-budget:]:
            k += 1
        log_text = build(k)
        assert cause in log_text[-budget:]
        assert cause not in build(k + 1)[-budget:]
        excerpt = extract_decisive_log_excerpt(log_text, max_chars=max_chars)
        assert cause in excerpt
        assert "config: retry-on-error: true" in excerpt
        assert len(excerpt) <= max_chars

    def test_terminal_marker_keeps_marker_and_lead_up(self) -> None:
        lines = [f"compiling module {i}" for i in range(300)]
        lines += ["src/lib.rs:12:5: mismatched types", "error: could not compile `app`"]
        lines += ["##[error]Process completed with exit code 101."]
        excerpt = extract_decisive_log_excerpt("\n".join(lines), max_chars=1500)
        assert "src/lib.rs:12:5: mismatched types" in excerpt
        assert "error: could not compile `app`" in excerpt
        assert len(excerpt) <= 1500

    @pytest.mark.parametrize(
        "marker_index", [pytest.param(0, id="fp-top"), pytest.param(150, id="fp-middle")]
    )
    @pytest.mark.parametrize("cause_distance", [15, 20, 30])
    def test_earlier_false_positive_does_not_starve_terminal_lead_up(
        self, marker_index: int, cause_distance: int
    ) -> None:
        """R4-1: inter-marker fill must not consume the lead-up budget."""
        cause = "linker: undefined reference to `charge_card'"
        terminal = "error: could not compile `app`"
        lines = [f"compiling module {i}" for i in range(300)]
        lines[marker_index:marker_index] = ["config: retry-on-error: true"]
        lines += [cause]
        lines += [f"compiling module tail {i}" for i in range(cause_distance - 1)]
        lines += [terminal, "##[error]Process completed with exit code 101."]
        log_text = "\n".join(lines)
        assert lines.index(terminal) - lines.index(cause) == cause_distance
        excerpt = extract_decisive_log_excerpt(log_text, max_chars=1500)
        assert cause in excerpt
        assert terminal in excerpt
        assert "config: retry-on-error: true" in excerpt
        assert len(excerpt) <= 1500

    @pytest.mark.parametrize(
        "fp_indices",
        [
            pytest.param((50,), id="1fp"),
            pytest.param((50, 120), id="2fp"),
            pytest.param((50, 120, 200), id="3fp"),
        ],
    )
    @pytest.mark.parametrize("cause_distance", [8, 12, 18, 25])
    def test_multiple_false_positives_do_not_starve_terminal_lead_up(
        self, fp_indices: tuple[int, ...], cause_distance: int
    ) -> None:
        """R5-1: earlier false-positive markers' context must not starve the lead-up.

        Uses realistic ~50-char lines, where each false positive's context costs
        the most budget. The documented guarantee is that the contiguous lead-up
        within ``max_chars // 2`` characters above the terminal marker is kept,
        however many false positives precede it; causes 8 and 12 lines up fall
        inside it, 18 and 25 lines up (~920 / ~1,275 chars) fall outside it, so
        for those only the reserved window itself is asserted.
        """
        max_chars = 1500
        cause = "linker: undefined reference to `charge_card'"
        terminal = "error: could not compile `app`"
        lines = [f"compiling module {i:03d} of the payments service crate" for i in range(300)]
        for offset, fp_index in enumerate(fp_indices):
            lines.insert(fp_index + offset, "config: retry-on-error: true")
        lines += [cause]
        lines += [
            f"compiling module {i:03d} of the payments service crate"
            for i in range(300, 300 + cause_distance - 1)
        ]
        lines += [terminal, "##[error]Process completed with exit code 101."]
        cause_idx, terminal_idx = lines.index(cause), lines.index(terminal)
        assert terminal_idx - cause_idx == cause_distance
        assert lines.count("config: retry-on-error: true") == len(fp_indices)

        excerpt = extract_decisive_log_excerpt("\n".join(lines), max_chars=max_chars)
        assert terminal in excerpt
        assert len(excerpt) <= max_chars

        # Widest contiguous lead-up ending at the terminal marker that fits in
        # half the budget must be kept verbatim.
        start = terminal_idx
        while len("\n".join(lines[start - 1 : terminal_idx + 1])) <= max_chars // 2:
            start -= 1
        assert "\n".join(lines[start : terminal_idx + 1]) in excerpt
        if cause_idx >= start:
            assert cause in excerpt
        else:
            assert cause_distance >= 18  # geometry guard: only the far causes are outside

    def test_leftover_budget_is_spent_on_lead_up_context(self) -> None:
        lines = [f"preparing step {i}" for i in range(40)]
        lines += ["error: something broke"]
        excerpt = extract_decisive_log_excerpt("\n".join(lines), max_chars=1500)
        # The whole short log fits, so nothing should be dropped.
        assert excerpt == "\n".join(lines)

    def test_log_with_only_cleanup_returns_empty(self) -> None:
        assert extract_decisive_log_excerpt(_cleanup_noise(500)) == ""


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
        decisive = "fatal: database schema version mismatch"
        assert decisive in res.summary
        assert decisive in res.probable_cause
        assert "Process completed with exit code" not in res.summary
        assert "Process completed with exit code" not in res.probable_cause
        excerpt = _runner_log_excerpt(res)
        assert excerpt.startswith(f"Step 'Migrate' failed: {decisive}")
        assert "fatal: database schema version mismatch" in excerpt
        assert "Invoking migrate.sh" in excerpt
        assert "Cleaning up orphan processes" not in excerpt
        assert "--unset-all" not in excerpt

    def test_diagnose_generic_falls_back_to_exit_line_when_no_cause_marker(self) -> None:
        log_text = "doing work\nstill working\n##[error]Process completed with exit code 2.\n"
        res = PipelineLogsSpecialist()._diagnose_generic(
            _make_incident("pipeline"), {"id": 1}, {"name": "Migrate"}, log_text
        )
        assert "Process completed with exit code 2." in res.summary
        assert "Process completed with exit code 2." in res.probable_cause
