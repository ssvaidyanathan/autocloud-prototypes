import json
from typing import Any, Optional

from ..contracts.report import Report
from .ack_content import (
    ACK_STATUS_ITALIC,
    ACK_SUBTITLE,
    FAILURE_SUBTITLE,
    INVESTIGATION_TITLE,
    WHAT_HAPPENS_NEXT_TITLE,
    get_failure_message,
    get_next_steps,
)


def _text_node(text: str, strong: bool = False, italic: bool = False) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "text", "text": text}
    marks: list[dict[str, str]] = []
    if strong:
        marks.append({"type": "strong"})
    if italic:
        marks.append({"type": "em"})
    if marks:
        node["marks"] = marks
    return node


def _paragraph_node(
    text: str, strong_prefix: Optional[str] = None, italic: bool = False
) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    if strong_prefix:
        content.append(_text_node(strong_prefix, strong=True))
    content.append(_text_node(text, italic=italic))
    return {"type": "paragraph", "content": content}


def _heading_node(text: str, level: int = 2) -> dict[str, Any]:
    return {
        "type": "heading",
        "attrs": {"level": level},
        "content": [_text_node(text)],
    }


def _panel_node(
    title: str,
    text: Optional[str] = None,
    panel_type: str = "warning",
    bullet_items: Optional[list[str]] = None,
) -> dict[str, Any]:
    panel_content: list[dict[str, Any]] = [
        {
            "type": "paragraph",
            "content": [_text_node(title, strong=True)],
        }
    ]
    if text:
        panel_content.append(
            {
                "type": "paragraph",
                "content": [_text_node(text)],
            }
        )
    if bullet_items:
        panel_content.append(_bullet_list_node(bullet_items))
    return {
        "type": "panel",
        "attrs": {"panelType": panel_type},
        "content": panel_content,
    }


def _bullet_list_node(items: list[str]) -> dict[str, Any]:
    return {
        "type": "bulletList",
        "content": [
            {
                "type": "listItem",
                "content": [{"type": "paragraph", "content": [_text_node(item)]}],
            }
            for item in items
        ],
    }


def _ordered_list_node(items: list[str]) -> dict[str, Any]:
    return {
        "type": "orderedList",
        "content": [
            {
                "type": "listItem",
                "content": [{"type": "paragraph", "content": [_text_node(item)]}],
            }
            for item in items
        ],
    }


def _table_node(headers: list[str], rows: list[list[str]]) -> dict[str, Any]:
    header_cells = [
        {
            "type": "tableHeader",
            "content": [_paragraph_node(h)],
        }
        for h in headers
    ]
    table_rows: list[dict[str, Any]] = [
        {
            "type": "tableRow",
            "content": header_cells,
        }
    ]
    for row in rows:
        row_cells = [
            {
                "type": "tableCell",
                "content": [_paragraph_node(cell)],
            }
            for cell in row
        ]
        table_rows.append(
            {
                "type": "tableRow",
                "content": row_cells,
            }
        )
    return {
        "type": "table",
        "attrs": {"isNumberColumnEnabled": False, "layout": "default"},
        "content": table_rows,
    }


def _code_block_node(text: str, language: str = "text") -> dict[str, Any]:
    return {
        "type": "codeBlock",
        "attrs": {"language": language},
        "content": [_text_node(text)],
    }


def render_jira_ack_adf() -> dict[str, Any]:
    """Generate standardized Jira acknowledgement ADF document from shared content."""
    content: list[dict[str, Any]] = [
        _heading_node(INVESTIGATION_TITLE, level=2),
        _heading_node(ACK_SUBTITLE, level=3),
        _paragraph_node(ACK_STATUS_ITALIC, italic=True),
        _heading_node(WHAT_HAPPENS_NEXT_TITLE, level=3),
        _bullet_list_node(get_next_steps("comment")),
    ]
    return {
        "version": 1,
        "type": "doc",
        "content": content,
    }


def render_jira_failure_adf(error: str) -> dict[str, Any]:
    """Generate standardized Jira failure ADF document from shared content."""
    content: list[dict[str, Any]] = [
        _heading_node(INVESTIGATION_TITLE, level=2),
        _heading_node(FAILURE_SUBTITLE, level=3),
        _panel_node(
            title=FAILURE_SUBTITLE,
            text=get_failure_message(error),
            panel_type="error",
        ),
    ]
    return {
        "version": 1,
        "type": "doc",
        "content": content,
    }


def report_to_adf(report: Report, incident_key: Optional[str] = None) -> dict[str, Any]:
    """Convert an SRE Report contract into a valid Atlassian Document Format (ADF) document.

    Maintains section and textual content parity with ServiceNow HTML report.
    """
    content: list[dict[str, Any]] = []

    # 1. Header & Subtitle
    content.append(_heading_node(INVESTIGATION_TITLE, level=2))
    content.append(_heading_node("Investigation findings", level=3))
    content.append(_paragraph_node("Automated analysis complete.", italic=True))

    # 2. Executive Summary & Impact
    content.append(_paragraph_node(report.summary, strong_prefix="Summary: "))
    content.append(_paragraph_node(report.impact, strong_prefix="Impact: "))

    # 3. Probable Root Cause Panel
    if report.probable_root_cause:
        conf_pct = int(round(report.probable_root_cause.confidence * 100))
        panel_title = f"Probable Root Cause (Confidence: {conf_pct}%)"
        content.append(
            _panel_node(panel_title, report.probable_root_cause.text, panel_type="warning")
        )

    # 4. Investigation Caveats & Telemetry Limitations
    if report.caveats:
        content.append(
            _panel_node(
                "Investigation Caveats & Telemetry Limitations",
                panel_type="error",
                bullet_items=report.caveats,
            )
        )

    # 5. Recent Changes
    if report.recent_changes:
        content.append(_heading_node("Recent Changes", level=3))
        headers = ["Timestamp (UTC)", "Description"]
        rows = [[c.timestamp.isoformat(), c.description] for c in report.recent_changes]
        content.append(_table_node(headers, rows))

    # 6. Recommended Actions
    if report.recommended_actions:
        content.append(_heading_node("Recommended Actions", level=3))
        content.append(_ordered_list_node(report.recommended_actions))

    # 7. Incident Timeline Highlights
    if report.timeline:
        content.append(_heading_node("Incident Timeline Highlights", level=3))
        headers = ["Timestamp (UTC)", "Description"]
        rows = [[t.timestamp.isoformat(), t.description] for t in report.timeline]
        content.append(_table_node(headers, rows))

    # 8. Full Investigation Report Attached Callout
    content.append(
        _panel_node(
            "Full Investigation Report Attached",
            text=(
                "For complete telemetry traces, metric graphs, logs, and raw evidence, "
                "refer to the attached standalone HTML report."
            ),
            panel_type="info",
        )
    )

    return {
        "version": 1,
        "type": "doc",
        "content": content,
    }


def chunk_adf(doc: dict[str, Any], max_chars: int = 32000) -> list[dict[str, Any]]:
    """Split an ADF document into multiple valid ADF documents if size exceeds max_chars."""
    serialized = json.dumps(doc)
    if len(serialized) <= max_chars:
        return [doc]

    raw_nodes = doc.get("content", [])
    if not raw_nodes:
        return [doc]

    chunks: list[list[dict[str, Any]]] = []
    current_chunk: list[dict[str, Any]] = []
    # Base overhead of {"version": 1, "type": "doc", "content": []} is ~50 chars
    current_len = 100

    for node in raw_nodes:
        node_len = len(json.dumps(node)) + 2  # account for comma and spacing
        if current_chunk and (current_len + node_len > max_chars):
            chunks.append(current_chunk)
            current_chunk = [node]
            current_len = 100 + node_len
        else:
            current_chunk.append(node)
            current_len += node_len

    if current_chunk:
        chunks.append(current_chunk)

    total_parts = len(chunks)
    result_docs: list[dict[str, Any]] = []
    for idx, chunk_content in enumerate(chunks):
        part_num = idx + 1
        part_header = _heading_node(f"SRE Report (Part {part_num}/{total_parts})", level=3)
        final_content = [part_header] + chunk_content
        result_docs.append(
            {
                "version": 1,
                "type": "doc",
                "content": final_content,
            }
        )

    return result_docs
