import html
from typing import Any, Optional

from ..contracts.report import Report


def render_servicenow_html(report: Report) -> str:
    """Render ServiceNow work note HTML format with inline styles."""
    lines = [
        (
            '<div style="font-family: -apple-system, BlinkMacSystemFont, sans-serif; '
            'font-size: 13px; line-height: 1.5; color: #24292f;">'
        ),
        (
            '  <div style="background: #0969da; color: #ffffff; padding: 8px 12px; '
            'font-weight: bold; border-radius: 4px 4px 0 0;">'
        ),
        "    🚀 Autocloud CI/CD Deployment Investigation Report",
        "  </div>",
        (
            '  <div style="border: 1px solid #d0d7de; border-top: none; padding: 12px; '
            'border-radius: 0 0 4px 4px; background: #ffffff;">'
        ),
    ]

    # Summary (safely truncate if necessary before HTML formatting)
    safe_summary = (
        report.summary[:2000] + "... (truncated)"
        if len(report.summary) > 2000
        else report.summary
    )
    lines.extend(
        [
            '    <div style="margin-bottom: 12px;">',
            (
                '      <div style="font-weight: 600; color: #57606a; '
                'margin-bottom: 4px;">Summary</div>'
            ),
            f"      <div>{html.escape(safe_summary)}</div>",
            "    </div>",
        ]
    )

    # Probable Root Cause
    if report.probable_root_cause:
        conf_pct = int(report.probable_root_cause.confidence * 100)
        prc_text = (
            report.probable_root_cause.text[:1000] + "... (truncated)"
            if len(report.probable_root_cause.text) > 1000
            else report.probable_root_cause.text
        )
        lines.extend(
            [
                (
                    '    <div style="margin-bottom: 12px; padding: 10px; background: #fff8c5; '
                    'border-left: 4px solid #bf8700; border-radius: 2px;">'
                ),
                '      <div style="font-weight: 600; color: #7d4e00;">Probable Root Cause</div>',
                (
                    f'      <div style="margin-top: 4px;">'
                    f"{html.escape(prc_text)}</div>"
                ),
                (
                    f'      <div style="margin-top: 4px; font-size: 11px; color: #57606a;">'
                    f"Confidence: <strong>{conf_pct}%</strong></div>"
                ),
                "    </div>",
            ]
        )

    # Timeline
    if report.timeline:
        lines.extend(
            [
                '    <div style="margin-bottom: 12px;">',
                (
                    '      <div style="font-weight: 600; color: #57606a; '
                    'margin-bottom: 4px;">Timeline</div>'
                ),
                '      <ul style="margin: 0; padding-left: 20px;">',
            ]
        )
        for item in report.timeline[:10]:
            ts = item.timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")
            desc = (
                item.description[:200] + "..."
                if len(item.description) > 200
                else item.description
            )
            lines.append(f"        <li><strong>{ts}</strong>: {html.escape(desc)}</li>")
        lines.extend(
            [
                "      </ul>",
                "    </div>",
            ]
        )

    # Recommended Actions
    if report.recommended_actions:
        lines.extend(
            [
                '    <div style="margin-bottom: 12px;">',
                (
                    '      <div style="font-weight: 600; color: #57606a; margin-bottom: 4px;">'
                    "Recommended Actions</div>"
                ),
                '      <ol style="margin: 0; padding-left: 20px;">',
            ]
        )
        for action in report.recommended_actions[:10]:
            action_text = action[:300] + "..." if len(action) > 300 else action
            lines.append(f"        <li>{html.escape(action_text)}</li>")
        lines.extend(
            [
                "      </ol>",
                "    </div>",
            ]
        )

    lines.extend(
        [
            "  </div>",
            "</div>",
        ]
    )
    return "\n".join(lines)


def chunk_servicenow_work_note(html_content: str, max_chars: int = 12000) -> list[str]:
    """Wrap ServiceNow report HTML into a single work note without exceeding limit."""
    wrapped = (
        f"[code]{html_content}[/code]" if not html_content.startswith("[code]") else html_content
    )
    if len(wrapped) <= max_chars:
        return [wrapped]

    # If content exceeds limit, safely escape plain text instead of mid-tag slicing
    max_inner = max_chars - len("[code]<div></div>[/code]") - 32
    safe_text = html.escape(html_content[:max_inner] + " ... [report truncated]")
    return [f"[code]<div>{safe_text}</div>[/code]"]


def render_standalone_html(report: Report, incident_ref: Optional[Any] = None) -> str:
    """Render standalone full HTML report for ticket attachments."""
    root_cause_html = ""
    if report.probable_root_cause:
        conf_pct = int(report.probable_root_cause.confidence * 100)
        root_cause_html = f"""
        <div class="callout warning">
            <h3>Probable Root Cause</h3>
            <p>{html.escape(report.probable_root_cause.text)}</p>
            <p class="meta">Confidence: <strong>{conf_pct}%</strong></p>
        </div>
        """

    timeline_html = ""
    if report.timeline:
        items = "\n".join(
            f"<li><code>{item.timestamp.strftime('%Y-%m-%d %H:%M:%S UTC')}</code>: "
            f"{html.escape(item.description)}</li>"
            for item in report.timeline
        )
        timeline_html = f"""
        <div class="section">
            <h2>Timeline</h2>
            <ul>{items}</ul>
        </div>
        """

    actions_html = ""
    if report.recommended_actions:
        items = "\n".join(f"<li>{html.escape(a)}</li>" for a in report.recommended_actions)
        actions_html = f"""
        <div class="section">
            <h2>Recommended Remediation Actions</h2>
            <ol>{items}</ol>
        </div>
        """

    ref_info = ""
    if incident_ref:
        ref_key = getattr(incident_ref, "external_key", str(incident_ref))
        ref_info = f"<p>Target Incident: <strong>{html.escape(ref_key)}</strong></p>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>CI/CD Deployment Investigation Report</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            color: #24292f;
            background: #f6f8fa;
            margin: 0;
            padding: 24px;
        }}
        .container {{
            max-width: 900px;
            margin: 0 auto;
            background: #ffffff;
            border: 1px solid #d0d7de;
            border-radius: 6px;
            padding: 32px;
            box-shadow: 0 1px 3px rgba(31,35,40,0.12);
        }}
        h1 {{
            color: #0969da;
            margin-top: 0;
            font-size: 24px;
            border-bottom: 1px solid #d0d7de;
            padding-bottom: 12px;
        }}
        h2 {{
            font-size: 18px;
            margin-top: 24px;
            margin-bottom: 8px;
            color: #1f2328;
        }}
        .callout {{
            padding: 16px;
            border-radius: 6px;
            margin: 16px 0;
        }}
        .callout.warning {{
            background-color: #fff8c5;
            border-left: 5px solid #bf8700;
        }}
        .callout h3 {{
            margin-top: 0;
            color: #7d4e00;
        }}
        .meta {{
            font-size: 12px;
            color: #57606a;
        }}
        code {{
            background: #f6f8fa;
            padding: 2px 6px;
            border-radius: 4px;
            font-size: 85%;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Autocloud CI/CD Deployment Investigation Report</h1>
        {ref_info}
        <div class="section">
            <h2>Executive Summary</h2>
            <p>{html.escape(report.summary)}</p>
        </div>
        {root_cause_html}
        {timeline_html}
        {actions_html}
    </div>
</body>
</html>
"""


__all__ = [
    "chunk_servicenow_work_note",
    "render_servicenow_html",
    "render_standalone_html",
]
