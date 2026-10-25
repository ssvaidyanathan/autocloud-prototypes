"""ServiceNow work notes must surface report caveats such as heuristic fallback."""

from bridge.app.contracts.report import Report
from bridge.app.templates.renderer import chunk_servicenow_work_note, render_servicenow_html

HEURISTIC_CAVEAT = (
    "Heuristic diagnosis because LLM synthesis failed (response truncated at the output "
    "token limit): the root cause and actions in this report come from deterministic "
    "specialist heuristics, not LLM synthesis, and no LLM fix proposal was generated."
)


def test_work_note_renders_heuristic_caveat_before_summary(sample_report: Report) -> None:
    sample_report.caveats = [HEURISTIC_CAVEAT]

    note = chunk_servicenow_work_note(render_servicenow_html(sample_report))[0]

    assert "Caveats" in note
    assert "Heuristic diagnosis because LLM synthesis failed" in note
    assert note.index("Caveats") < note.index("Summary")


def test_work_note_caveat_survives_truncation(sample_report: Report) -> None:
    sample_report.caveats = [HEURISTIC_CAVEAT]
    sample_report.recommended_actions = ["x" * 400] * 200

    note = chunk_servicenow_work_note(render_servicenow_html(sample_report), max_chars=3000)[0]

    assert "Heuristic diagnosis because LLM synthesis failed" in note


def test_work_note_caveats_escaped(sample_report: Report) -> None:
    sample_report.caveats = ["<script>alert(1)</script>"]

    html_out = render_servicenow_html(sample_report)

    assert "<script>" not in html_out
    assert "&lt;script&gt;" in html_out


def test_work_note_without_caveats_has_no_caveat_block(sample_report: Report) -> None:
    sample_report.caveats = []

    assert "Caveats" not in render_servicenow_html(sample_report)
