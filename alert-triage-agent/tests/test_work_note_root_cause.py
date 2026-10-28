"""QA r2 S2 nits: the ServiceNow work note states the probable root cause, and the description
splits observations only on the ' | ' join separator (never on ';' inside a sentence)."""

from agent.app.contracts.report import (
    WORK_NOTE_ROOT_CAUSE_MAX_LENGTH,
    InvestigationReport,
    ProbableRootCause,
)

S2_ROOT_CAUSE = (
    "Service degradation on `ata-inventory-api` (peak per-minute mean latency: 6547ms). "
    "Apigee proxy `ata-inventory-v1` (revision 138) returned 357 HTTP 504 responses with fault "
    "`messaging.adaptors.http.flow.GatewayTimeout` (fault source: target). Requests on "
    "ata-inventory-v1 -> ata-inventory-api did not complete within the proxy's target timeout."
)

S2_ORIGIN_OBS = (
    "Origin selection: ata-inventory-api is upstream of ata-orders-api; "
    "The first alert arrived on ata-orders-api; arrival order was not used to pick the origin."
)


def _report(**kw) -> InvestigationReport:
    base = dict(
        group_id="ata-orders-latency-ata-orders-api-20261027t122935z",
        summary="Elevated latency on ata-inventory-api",
        observations=f"Latency 6547ms on ata-inventory-api | {S2_ORIGIN_OBS}",
        origin="ata-inventory-api",
        blast_radius=["ata-inventory-api", "ata-orders-api"],
        priority="P2",
        probable_root_cause=ProbableRootCause(text=S2_ROOT_CAUSE, confidence=0.81),
    )
    base.update(kw)
    return InvestigationReport(**base)


def test_work_note_contains_root_cause_with_gateway_timeout():
    note = _report().build_executive_summary_html()
    assert "<li><b>Probable Root Cause:</b> " in note
    assert "<code>messaging.adaptors.http.flow.GatewayTimeout</code>" in note
    assert "357 HTTP 504 responses" in note
    # Between Systems and Observations.
    assert note.index("<b>Systems:</b>") < note.index("Probable Root Cause") < note.index("<b>Observations:</b>")


def test_work_note_root_cause_is_escaped_and_one_line():
    note = _report(
        probable_root_cause=ProbableRootCause(text="Bad <script>\n  [/code] x & y", confidence=0.5)
    ).build_executive_summary_html()
    line = next(ln for ln in note.splitlines() if "Probable Root Cause" in ln)
    assert "&lt;script&gt; &#91;/code&#93; x &amp; y" in line
    assert "<script>" not in note
    assert note.count("[/code]") == 1


def test_work_note_long_root_cause_is_cut_and_says_so():
    long_text = ("word " * 400).strip()
    note = _report(probable_root_cause=ProbableRootCause(text=long_text, confidence=0.5)).build_executive_summary_html()
    line = next(ln for ln in note.splitlines() if "Probable Root Cause" in ln)
    assert line.endswith("... (full text in the attached report)</li>")
    body = line.split("</b> ", 1)[1]
    assert len(body) <= WORK_NOTE_ROOT_CAUSE_MAX_LENGTH + len("... (full text in the attached report)</li>")


def test_description_keeps_sentences_with_semicolons_in_one_bullet():
    desc = _report().build_description(project_id="cloud-assist-fde-2", incidents_bucket="b")
    assert f"  - {S2_ORIGIN_OBS}" in desc
    assert "\n  - The first alert arrived" not in desc
    assert "\n  - arrival order was not used" not in desc
    assert "  - Latency 6547ms on ata-inventory-api" in desc


def test_description_still_splits_on_pipe_separator():
    desc = _report(observations="A on x | B on y | C; with detail").build_description(
        project_id="cloud-assist-fde-2", incidents_bucket="b"
    )
    assert "  - A on x\n  - B on y\n  - C; with detail\n" in desc
