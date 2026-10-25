import json
import logging
import re
from typing import Any, Optional

import google.auth
import httpx
from google.auth.transport.requests import Request

from .config import AgentConfig, get_settings

logger = logging.getLogger(__name__)


_CODE_FENCE_OPEN_RE = re.compile(r"^```(?:json)?\s*", re.IGNORECASE)
_CODE_FENCE_CLOSE_RE = re.compile(r"\s*```$")


def parse_synthesis_payload(raw_text: str) -> Optional[dict[str, Any]]:
    """Parse a synthesizer model response into a synthesis dict.

    Single deterministic parse path: strip markdown code fences, trim any
    wrapper text outside the outermost JSON object, decode, and validate.
    Returns None (with a warning) when the payload is not a valid synthesis
    object; no heuristic field extraction is attempted.
    """
    if not raw_text or not raw_text.strip():
        return None

    text = _CODE_FENCE_OPEN_RE.sub("", raw_text.strip())
    text = _CODE_FENCE_CLOSE_RE.sub("", text).strip()

    first_idx = text.find("{")
    last_idx = text.rfind("}")
    if first_idx == -1 or last_idx < first_idx:
        logger.warning(
            "Synthesis response contained no JSON object (len=%d): %.200r",
            len(raw_text),
            raw_text,
        )
        return None

    json_str = text[first_idx : last_idx + 1]
    try:
        parsed = json.loads(json_str)
        if not isinstance(parsed, dict) or "root_cause" not in parsed:
            logger.warning(
                "Synthesis response JSON is not an object with root_cause: %.200r",
                json_str,
            )
            return None

        causal = parsed.get("causal_remediation")
        if causal is None:
            causal = parsed.get("remediation_actions", [])
        causal_list = [str(a).strip() for a in (causal or []) if str(a).strip()]
        unrelated_list = [
            str(o).strip() for o in (parsed.get("unrelated_observations") or []) if str(o).strip()
        ]
        synthesis: dict[str, Any] = {
            "root_cause": str(parsed["root_cause"]).strip(),
            "causal_remediation": causal_list,
            "unrelated_observations": unrelated_list,
            "remediation_actions": causal_list,
            "confidence": float(parsed.get("confidence", 0.95)),
        }
        if parsed.get("fix_proposal") is not None:
            synthesis["fix_proposal"] = parsed["fix_proposal"]
        return synthesis
    except Exception as exc:
        logger.warning(
            "Failed to parse synthesis response JSON (%s: %s): %.200r",
            type(exc).__name__,
            exc,
            json_str,
        )
        return None


def _config_value(cfg: Any, field_name: str) -> Any:
    """Read a setting from cfg, defaulting to the AgentConfig field default."""
    return getattr(cfg, field_name, AgentConfig.model_fields[field_name].default)


def generate_gemini_synthesis(
    failing_job: dict[str, Any],
    failing_step: dict[str, Any],
    log_text: str,
    config: Optional[AgentConfig] = None,
    client: Optional[httpx.Client] = None,
    specialist_findings: Optional[dict[str, Any]] = None,
    diagnostic_summary: Optional[str] = None,
    synthesis_status: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """Generate grounded causal diagnosis using Vertex AI Gemini via REST API.

    When ``synthesis_status`` is provided it is filled with ``outcome`` (one of
    ``ok``, ``truncated``, ``unparseable``, ``empty``, ``error``, ``unavailable``)
    and ``finish_reason`` so callers can surface synthesis failures in the report.
    """
    cfg = config or get_settings()
    status: dict[str, Any] = synthesis_status if synthesis_status is not None else {}
    status["outcome"] = "unavailable"
    status["finish_reason"] = None
    max_output_tokens = _config_value(cfg, "synthesis_max_output_tokens")
    thinking_budget = _config_value(cfg, "synthesis_thinking_budget")

    token = None
    project_id = getattr(cfg, "gcp_project", None)

    try:
        credentials, default_project = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        if not credentials.valid:
            credentials.refresh(Request())
        token = credentials.token
        if not project_id or project_id == "local-project":
            project_id = default_project
    except Exception as auth_exc:
        logger.info("Vertex AI ADC not available or refresh failed: %s", auth_exc)
        return None

    if not token or not project_id or project_id == "local-project":
        logger.info("Missing valid GCP project or token for Vertex AI")
        return None

    location = getattr(cfg, "vertex_ai_location", "us-central1")
    model = getattr(cfg, "gemini_model", None)
    if not model:
        logger.info("Gemini model name not configured")
        return None

    endpoint = (
        f"https://{location}-aiplatform.googleapis.com/v1/projects/{project_id}/"
        f"locations/{location}/publishers/google/models/{model}:generateContent"
    )

    job_name = failing_job.get("name", "pipeline")
    step_name = failing_step.get("name", "execution")
    if specialist_findings and specialist_findings.get("runner_log_excerpt"):
        log_excerpt = specialist_findings["runner_log_excerpt"]
    else:
        from .specialists.pipeline_logs import extract_decisive_log_excerpt

        log_excerpt = extract_decisive_log_excerpt(log_text, max_chars=4000)

    findings_parts = []
    if diagnostic_summary:
        findings_parts.append(f"Diagnostic Summary: {diagnostic_summary}")
    if specialist_findings:
        cause = specialist_findings.get("probable_cause")
        if cause:
            findings_parts.append(f"Probable Cause: {cause}")
        sumry = specialist_findings.get("summary")
        if sumry and sumry != diagnostic_summary:
            findings_parts.append(f"Summary: {sumry}")
        findings_list = specialist_findings.get("findings")
        if findings_list:
            findings_parts.append("Findings:\n" + "\n".join(f"- {f}" for f in findings_list))
        linter_breakdown = specialist_findings.get("linter_breakdown")
        if linter_breakdown:
            findings_parts.append(f"Linter Breakdown:\n{linter_breakdown}")
        diff_info = specialist_findings.get("diff_info")
        if diff_info:
            findings_parts.append(f"Git Diff Context:\n{diff_info}")
        container_logs = specialist_findings.get("container_logs")
        if container_logs:
            findings_parts.append(f"Container Logs Before Termination:\n{container_logs}")
        telemetry_info = specialist_findings.get("telemetry_info")
        if telemetry_info:
            findings_parts.append(f"Runtime Telemetry:\n{telemetry_info}")
        runner_log_excerpt = specialist_findings.get("runner_log_excerpt")
        if runner_log_excerpt:
            findings_parts.append(f"Runner Log Evidence Excerpt:\n{runner_log_excerpt}")
        structured_errors = specialist_findings.get("structured_errors")
        if structured_errors:
            lines = [
                f"- [{err.get('code')}] {err.get('file')}:{err.get('line')}: {err.get('message')}"
                for err in structured_errors[:20]
            ]
            findings_parts.append("Detailed Violations:\n" + "\n".join(lines))

    findings_context = ""
    if findings_parts:
        findings_context = "\nSpecialist Analysis & Findings:\n" + "\n".join(findings_parts) + "\n"

    from .agent_config import get_agent_config

    prompt_template = get_agent_config().get_prompt_template("sre_synthesis")
    prompt = prompt_template.format(
        job_name=job_name,
        step_name=step_name,
        findings_context=findings_context,
        log_excerpt=log_excerpt,
    )
    try:
        patch_guidance = get_agent_config().get_patch_instruction("synthesizer")
    except (KeyError, ValueError):
        patch_guidance = ""
    if patch_guidance:
        prompt = "\n".join((prompt, patch_guidance, ""))

    request_body = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": max_output_tokens,
            "responseMimeType": "application/json",
            "thinkingConfig": {"thinkingBudget": thinking_budget},
        },
    }

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    owns_client = client is None
    c = client or httpx.Client(timeout=30.0)
    try:
        resp = c.post(endpoint, json=request_body, headers=headers)
        if resp.status_code != 200:
            logger.warning(
                "Vertex AI Gemini API returned status %d: %s", resp.status_code, resp.text
            )
            status["outcome"] = "error"
            return None

        result_data = resp.json()
        candidates = result_data.get("candidates", [])
        if not candidates:
            logger.warning("Vertex AI Gemini response had no candidates: %s", result_data)
            status["outcome"] = "empty"
            return None

        first_candidate = candidates[0]
        finish_reason = first_candidate.get("finishReason")
        status["finish_reason"] = finish_reason
        truncated = finish_reason == "MAX_TOKENS"
        if truncated:
            logger.warning(
                "Vertex AI Gemini synthesis hit MAX_TOKENS (maxOutputTokens=%d, "
                "thinkingBudget=%d); response may be truncated",
                max_output_tokens,
                thinking_budget,
            )
        content_parts = first_candidate.get("content", {}).get("parts", [])
        raw_text = "".join(
            p.get("text", "") for p in content_parts if not p.get("thought", False)
        ).strip()
        if not raw_text:
            status["outcome"] = "truncated" if truncated else "empty"
            return None

        parsed = parse_synthesis_payload(raw_text)
        if parsed is None:
            logger.warning(
                "Vertex AI Gemini synthesis response unparseable (finishReason=%s, len=%d)",
                finish_reason,
                len(raw_text),
            )
            status["outcome"] = "truncated" if truncated else "unparseable"
            return None
        status["outcome"] = "truncated" if truncated else "ok"
        return parsed
    except Exception as exc:
        logger.warning("Vertex AI Gemini synthesis call failed: %s", exc)
        status["outcome"] = "error"
        return None
    finally:
        if owns_client:
            c.close()
