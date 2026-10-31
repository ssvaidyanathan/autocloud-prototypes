import json
import re
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


_INLINE_MARKDOWN_RE = re.compile(
    r"(\[(?P<link_text>[^\]]+)\]\((?P<link_url>[^)]+)\))|"
    r"(\*\*\*(?P<bi_text>[^*]+)\*\*\*)|"
    r"(\*\*(?P<bold_text>[^*]+)\*\*)|"
    r"(\*(?P<italic_text>[^*]+)\*)"
)


def _parse_inline_text(text: str) -> list[dict[str, Any]]:
    """Parse inline markdown (links, bold, italic) into ADF text nodes."""
    if not text:
        return []
    nodes: list[dict[str, Any]] = []
    last_idx = 0
    for m in _INLINE_MARKDOWN_RE.finditer(text):
        start, end = m.span()
        if start > last_idx:
            nodes.append({"type": "text", "text": text[last_idx:start]})
        if m.group("link_text"):
            lt = m.group("link_text")
            marks: list[dict[str, Any]] = [{"type": "link", "attrs": {"href": m.group("link_url")}}]
            if lt.startswith("**") and lt.endswith("**"):
                lt = lt[2:-2]
                marks.append({"type": "strong"})
            nodes.append({"type": "text", "text": lt, "marks": marks})
        elif m.group("bi_text"):
            nodes.append(
                {
                    "type": "text",
                    "text": m.group("bi_text"),
                    "marks": [{"type": "strong"}, {"type": "em"}],
                }
            )
        elif m.group("bold_text"):
            bt = m.group("bold_text")
            lm = re.match(r"^\[([^\]]+)\]\(([^)]+)\)$", bt)
            if lm:
                nodes.append(
                    {
                        "type": "text",
                        "text": lm.group(1),
                        "marks": [
                            {"type": "strong"},
                            {"type": "link", "attrs": {"href": lm.group(2)}},
                        ],
                    }
                )
            else:
                nodes.append({"type": "text", "text": bt, "marks": [{"type": "strong"}]})
        elif m.group("italic_text"):
            nodes.append(
                {"type": "text", "text": m.group("italic_text"), "marks": [{"type": "em"}]}
            )
        last_idx = end

    if last_idx < len(text):
        nodes.append({"type": "text", "text": text[last_idx:]})
    return nodes


def markdown_to_adf(markdown_text: str) -> dict[str, Any]:
    """Convert Markdown text to Atlassian Document Format (ADF) document.

    Converts:
      - Headings (#, ##, ###) -> ADF heading nodes (level 1-6)
      - Bullet points (- or *) -> ADF bulletList
      - Tables (| col1 | col2 |) -> ADF table (tableHeader and tableCell)
      - Bold (**text**), Italic (*text*), Links ([text](url)) -> ADF text marks
      - Standard paragraphs -> ADF paragraph
      - Empty text -> ADF doc with empty paragraph
    """
    if not markdown_text or not markdown_text.strip():
        return {
            "version": 1,
            "type": "doc",
            "content": [{"type": "paragraph", "content": []}],
        }

    lines = markdown_text.splitlines()
    content: list[dict[str, Any]] = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        # 1. Heading (#, ##, ###, ...)
        heading_match = re.match(r"^(#{1,6})(?:\s*(.*))?$", stripped)
        if heading_match:
            hashes, raw_htext = heading_match.groups()
            htext = (raw_htext or "").strip()
            if not htext:
                htext = "Incident Section"
            level = min(len(hashes), 6)
            inline_content = _parse_inline_text(htext)
            content.append(
                {
                    "type": "heading",
                    "attrs": {"level": level},
                    "content": inline_content
                    if inline_content
                    else [{"type": "text", "text": htext}],
                }
            )
            i += 1
            continue

        # 2. Table (| ... |)
        if stripped.startswith("|") and stripped.endswith("|"):
            table_lines: list[str] = []
            while i < n and lines[i].strip().startswith("|") and lines[i].strip().endswith("|"):
                table_lines.append(lines[i].strip())
                i += 1

            headers: list[str] = []
            rows: list[list[str]] = []
            has_separator = False
            for t_idx, t_line in enumerate(table_lines):
                cells = [c.strip() for c in t_line.split("|")[1:-1]]
                if all(re.match(r"^:?-+:?$", c) for c in cells if c):
                    has_separator = True
                    continue
                if t_idx == 0 or (not headers and not has_separator):
                    headers = cells
                else:
                    rows.append(cells)

            table_rows: list[dict[str, Any]] = []
            if headers:
                header_cells = [
                    {
                        "type": "tableHeader",
                        "content": [{"type": "paragraph", "content": _parse_inline_text(h)}],
                    }
                    for h in headers
                ]
                table_rows.append({"type": "tableRow", "content": header_cells})

            for row in rows:
                cell_nodes = [
                    {
                        "type": "tableCell",
                        "content": [{"type": "paragraph", "content": _parse_inline_text(c)}],
                    }
                    for c in row
                ]
                table_rows.append({"type": "tableRow", "content": cell_nodes})

            content.append(
                {
                    "type": "table",
                    "attrs": {"isNumberColumnEnabled": False, "layout": "default"},
                    "content": table_rows,
                }
            )
            continue

        # 3. Bullet list (- or *)
        if re.match(r"^[\-\*]\s+(.*)$", stripped):
            list_items: list[dict[str, Any]] = []
            while i < n:
                b_match = re.match(r"^[\-\*]\s+(.*)$", lines[i].strip())
                if not b_match:
                    break
                item_text = b_match.group(1)
                list_items.append(
                    {
                        "type": "listItem",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": _parse_inline_text(item_text),
                            }
                        ],
                    }
                )
                i += 1

            content.append(
                {
                    "type": "bulletList",
                    "content": list_items,
                }
            )
            continue

        # 4. Standard Paragraph (collect consecutive non-block lines)
        para_lines = [stripped]
        i += 1
        while i < n:
            next_stripped = lines[i].strip()
            if not next_stripped:
                break
            if (
                next_stripped.startswith("#")
                or (next_stripped.startswith("|") and next_stripped.endswith("|"))
                or re.match(r"^[\-\*]\s+", next_stripped)
            ):
                break
            para_lines.append(next_stripped)
            i += 1

        para_text = " ".join(para_lines)
        content.append(
            {
                "type": "paragraph",
                "content": _parse_inline_text(para_text),
            }
        )

    if not content:
        content.append({"type": "paragraph", "content": []})

    return {
        "version": 1,
        "type": "doc",
        "content": content,
    }


def update_adf_root_causes(adf_doc: dict[str, Any], root_causes_text: str) -> bool:
    """Find Root Causes section in an ADF document and update it with root_causes_text."""
    if not isinstance(adf_doc, dict) or adf_doc.get("type") != "doc":
        return False

    content: list[dict[str, Any]] = adf_doc.get("content", [])
    rc_idx = -1

    for i, node in enumerate(content):
        texts: list[str] = []
        if node.get("type") in ("paragraph", "heading"):
            for c in node.get("content", []):
                if c.get("type") == "text":
                    texts.append(c.get("text", ""))
        full_text = " ".join(texts)
        if "Root Causes" in full_text:
            rc_idx = i
            break

    new_node = {
        "type": "paragraph",
        "content": _parse_inline_text(root_causes_text),
    }

    if rc_idx != -1:
        if rc_idx + 1 < len(content):
            next_node = content[rc_idx + 1]
            if next_node.get("type") in ("paragraph", "bulletList"):
                content[rc_idx + 1] = new_node
            else:
                content.insert(rc_idx + 1, new_node)
        else:
            content.append(new_node)
        return True

    # If no existing Root Causes section found, append one
    content.extend(
        [
            {
                "type": "heading",
                "attrs": {"level": 2},
                "content": [{"type": "text", "text": "Root Causes"}],
            },
            new_node,
        ]
    )
    return True
