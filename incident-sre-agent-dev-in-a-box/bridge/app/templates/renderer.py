from pathlib import Path
from typing import Any, Optional

from jinja2 import Environment, FileSystemLoader

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
from .jira_adf import (
    chunk_adf,
    render_jira_ack_adf,
    render_jira_failure_adf,
    report_to_adf,
)

_TEMPLATES_DIR = Path(__file__).resolve().parent

# Configure Jinja2 environment with strict autoescaping
_jinja_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=True,
    trim_blocks=True,
    lstrip_blocks=True,
)


def render_servicenow_html(report: Report) -> str:
    """Render ServiceNow work note HTML template with inline styles and strict autoescaping."""
    template = _jinja_env.get_template("servicenow_report.html.j2")
    return template.render(report=report)


def render_servicenow_ack_html() -> str:
    """Render standardized ServiceNow acknowledgement note HTML template from shared content."""
    template = _jinja_env.get_template("servicenow_ack.html.j2")
    return template.render(
        title=INVESTIGATION_TITLE,
        subtitle=ACK_SUBTITLE,
        status_italic=ACK_STATUS_ITALIC,
        next_steps_title=WHAT_HAPPENS_NEXT_TITLE,
        next_steps=get_next_steps("work note"),
    )


def render_servicenow_failure_html(error: str) -> str:
    """Render standardized ServiceNow failure note HTML template from shared content."""
    template = _jinja_env.get_template("servicenow_failure.html.j2")
    return template.render(
        title=INVESTIGATION_TITLE,
        subtitle=FAILURE_SUBTITLE,
        message=get_failure_message(error),
    )


def chunk_servicenow_work_note(html: str, max_chars: int = 12000) -> list[str]:
    """Wrap ServiceNow report HTML into a single work note without chunking (zero chunking)."""
    wrapped = f"[code]{html}[/code]" if not html.startswith("[code]") else html
    if len(wrapped) > max_chars:
        closing = "</div>[/code]"
        wrapped = wrapped[: max_chars - len(closing)] + closing
    return [wrapped]


def render_standalone_html(report: Report, incident_ref: Optional[Any] = None) -> str:
    """Render standalone full HTML report for ticket attachments."""
    template = _jinja_env.get_template("standalone_report.html.j2")
    return template.render(report=report, incident_ref=incident_ref)


def render_jira_adf(report: Report, incident_key: Optional[str] = None) -> dict[str, Any]:
    """Generate Atlassian Document Format document for Jira comments."""
    return report_to_adf(report, incident_key=incident_key)


__all__ = [
    "render_servicenow_html",
    "render_servicenow_ack_html",
    "render_servicenow_failure_html",
    "chunk_servicenow_work_note",
    "render_standalone_html",
    "render_jira_adf",
    "render_jira_ack_adf",
    "render_jira_failure_adf",
    "chunk_adf",
]
