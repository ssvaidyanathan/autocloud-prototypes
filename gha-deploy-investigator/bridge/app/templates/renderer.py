import html
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from jinja2 import Environment, FileSystemLoader

from ..contracts.incident import Incident
from ..contracts.report import Report

_TEMPLATES_DIR = Path(__file__).resolve().parent

# Configure Jinja2 environment with strict autoescaping
_jinja_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=True,
    trim_blocks=True,
    lstrip_blocks=True,
)


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
        report.summary[:2000] + "... (truncated)" if len(report.summary) > 2000 else report.summary
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
                (f'      <div style="margin-top: 4px;">{html.escape(prc_text)}</div>'),
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
                item.description[:200] + "..." if len(item.description) > 200 else item.description
            )
            lines.append(f"        <li><strong>{ts}</strong>: {html.escape(desc)}</li>")
        lines.extend(
            [
                "      </ul>",
                "    </div>",
            ]
        )

    # Recommended Actions
    remediation_actions = report.causal_remediation or report.recommended_actions
    if remediation_actions:
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
        for action in remediation_actions[:10]:
            action_text = action[:300] + "..." if len(action) > 300 else action
            lines.append(f"        <li>{html.escape(action_text)}</li>")
        lines.extend(
            [
                "      </ol>",
                "    </div>",
            ]
        )

    # Unrelated Observations
    if report.unrelated_observations:
        lines.extend(
            [
                '    <div style="margin-bottom: 12px;">',
                (
                    '      <div style="font-weight: 600; color: #57606a; margin-bottom: 4px;">'
                    "Unrelated Observations</div>"
                ),
                '      <ul style="margin: 0; padding-left: 20px;">',
            ]
        )
        for obs in report.unrelated_observations[:10]:
            obs_text = obs[:300] + "..." if len(obs) > 300 else obs
            lines.append(f"        <li>{html.escape(obs_text)}</li>")
        lines.extend(
            [
                "      </ul>",
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


def render_standalone_html(
    report: Report,
    incident_ref: Optional[Any] = None,
    incident: Optional[Incident] = None,
    config: Optional[Any] = None,
    generated_at: Optional[datetime] = None,
) -> str:
    """Render standalone elaborate diagnostic dossier HTML report for ticket attachments."""
    if incident is None and isinstance(incident_ref, Incident):
        incident = incident_ref

    template = _jinja_env.get_template("standalone_report.html.j2")

    # Resolve incident fields safely
    ext_key = None
    if incident_ref and hasattr(incident_ref, "external_key"):
        ext_key = incident_ref.external_key
    elif incident and incident.external_key:
        ext_key = incident.external_key

    ext_url = None
    if incident_ref and hasattr(incident_ref, "external_url"):
        ext_url = incident_ref.external_url
    elif incident and incident.external_url:
        ext_url = incident.external_url

    repo = incident.github_repo if incident else None
    branch = incident.github_branch if incident else None
    head_sha = incident.github_head_sha if incident else None
    short_sha = head_sha[:8] if head_sha else None
    actor = (incident.github_actor or incident.reporter) if incident else None
    failing_stage = (
        incident.github_failing_stage
        or (report.metadata.get("failing_stage") if report and report.metadata else None)
        if incident
        else (report.metadata.get("failing_stage") if report and report.metadata else None)
    )
    severity = incident.severity if incident else "P3"
    run_id = incident.github_run_id if incident else None

    # Resolve links
    run_url = None
    if incident and incident.external_url and "github.com" in incident.external_url:
        run_url = incident.external_url
    elif repo and run_id:
        run_url = f"https://github.com/{repo}/actions/runs/{run_id}"

    check_run_url = None
    if repo and head_sha:
        check_run_url = f"https://github.com/{repo}/commit/{head_sha}/checks"

    servicenow_url = None
    if ext_url and "nav_to.do" in ext_url:
        servicenow_url = ext_url
    elif incident_ref and hasattr(incident_ref, "external_id") and config:
        base_url = (getattr(config, "servicenow_instance_url", None) or "").rstrip("/")
        if base_url:
            sys_id = incident_ref.external_id
            servicenow_url = f"{base_url}/nav_to.do?uri=incident.do?sys_id={sys_id}"

    # Resolve model name safely from config or metadata (zero hardcoded literals)
    model_name = None
    if config and hasattr(config, "gemini_model") and config.gemini_model:
        model_name = config.gemini_model
    elif report.metadata and "model" in report.metadata:
        model_name = report.metadata["model"]
    elif "GEMINI_MODEL" in os.environ:
        model_name = os.environ["GEMINI_MODEL"]

    gen_time = (
        (generated_at or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M:%S UTC")
    )

    return template.render(
        report=report,
        incident=incident,
        incident_ref=incident_ref,
        incident_key=ext_key,
        repo=repo,
        branch=branch,
        head_sha=head_sha,
        short_sha=short_sha,
        actor=actor,
        failing_stage=failing_stage,
        severity=severity,
        run_id=run_id,
        run_url=run_url,
        check_run_url=check_run_url,
        servicenow_url=servicenow_url,
        model_name=model_name,
        generated_at=gen_time,
    )


__all__ = [
    "chunk_servicenow_work_note",
    "render_servicenow_html",
    "render_standalone_html",
]

