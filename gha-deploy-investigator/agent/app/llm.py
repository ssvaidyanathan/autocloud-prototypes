import json
import logging
from typing import Any, Optional

import google.auth
import httpx
from google.auth.transport.requests import Request

from .config import AgentConfig, get_settings

logger = logging.getLogger(__name__)


def generate_gemini_synthesis(
    failing_job: dict[str, Any],
    failing_step: dict[str, Any],
    log_text: str,
    config: Optional[AgentConfig] = None,
    client: Optional[httpx.Client] = None,
    specialist_findings: Optional[dict[str, Any]] = None,
    diagnostic_summary: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Generate grounded causal diagnosis using Vertex AI Gemini via REST API."""
    cfg = config or get_settings()

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
    log_excerpt = log_text[-4000:] if len(log_text) > 4000 else log_text

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
            "maxOutputTokens": 4096,
            "responseMimeType": "application/json",
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
            return None

        result_data = resp.json()
        candidates = result_data.get("candidates", [])
        if not candidates:
            logger.warning("Vertex AI Gemini response had no candidates: %s", result_data)
            return None

        first_candidate = candidates[0]
        content_parts = first_candidate.get("content", {}).get("parts", [])
        if not content_parts:
            return None

        raw_text = content_parts[0].get("text", "").strip()
        if not raw_text:
            return None

        import re

        clean_text = raw_text.strip()
        clean_text = re.sub(r"^```(?:json)?\s*", "", clean_text, flags=re.IGNORECASE)
        clean_text = re.sub(r"\s*```$", "", clean_text)
        clean_text = clean_text.strip()

        try:
            parsed = json.loads(clean_text)
            if isinstance(parsed, dict) and "root_cause" in parsed:
                causal = parsed.get("causal_remediation")
                if causal is None:
                    causal = parsed.get("remediation_actions", [])
                causal_list = [str(a).strip() for a in (causal or []) if str(a).strip()]
                unrelated_list = [
                    str(o).strip()
                    for o in parsed.get("unrelated_observations", [])
                    if str(o).strip()
                ]
                synthesis: dict[str, Any] = {
                    "root_cause": str(parsed["root_cause"]).strip(),
                    "causal_remediation": causal_list,
                    "unrelated_observations": unrelated_list,
                    "remediation_actions": causal_list,
                    "confidence": float(parsed.get("confidence", 0.95)),
                }
                if "fix_proposal" in parsed:
                    synthesis["fix_proposal"] = parsed["fix_proposal"]
                return synthesis
        except Exception:
            # Fallback: search for first { and last }
            m = re.search(r"(\{.*\})", clean_text, re.DOTALL)
            if m:
                try:
                    parsed = json.loads(m.group(1))
                    if isinstance(parsed, dict) and "root_cause" in parsed:
                        causal = parsed.get("causal_remediation")
                        if causal is None:
                            causal = parsed.get("remediation_actions", [])
                        causal_list = [str(a).strip() for a in (causal or []) if str(a).strip()]
                        unrelated_list = [
                            str(o).strip()
                            for o in parsed.get("unrelated_observations", [])
                            if str(o).strip()
                        ]
                        return {
                            "root_cause": str(parsed["root_cause"]).strip(),
                            "causal_remediation": causal_list,
                            "unrelated_observations": unrelated_list,
                            "remediation_actions": causal_list,
                            "confidence": float(parsed.get("confidence", 0.95)),
                        }
                except Exception:
                    pass

        # Defensive extraction for broken or truncated JSON:
        # Extract root_cause without returning raw JSON syntax/brackets.
        extracted_cause = None
        rc_match = re.search(r'"root_cause"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"?', clean_text)
        if rc_match:
            extracted_cause = rc_match.group(1).replace(r"\"", '"').strip()

        if not extracted_cause:
            # Strip brackets, braces, and JSON keys to obtain clean prose
            cleaned_prose = re.sub(r"^\{\s*\"root_cause\"\s*:\s*\"?", "", clean_text)
            cleaned_prose = re.sub(
                r"\"?\s*,?\s*\"(?:causal_remediation|remediation_actions|unrelated_observations)\".*$",
                "",
                cleaned_prose,
                flags=re.DOTALL,
            )
            cleaned_prose = re.sub(r"\"?\s*\}\s*$", "", cleaned_prose)
            extracted_cause = cleaned_prose.strip(' \t\n\r"{}')

        extracted_actions = []
        rem_match = re.search(r'"causal_remediation"\s*:\s*\[(.*?)\]', clean_text, re.DOTALL)
        if not rem_match:
            rem_match = re.search(r'"remediation_actions"\s*:\s*\[(.*?)\]', clean_text, re.DOTALL)
        if rem_match:
            actions_raw = rem_match.group(1)
            for act in re.findall(r'"([^"\\]*(?:\\.[^"\\]*)*)"', actions_raw):
                extracted_actions.append(act.replace(r"\"", '"').strip())

        extracted_unrelated = []
        unrelated_match = re.search(
            r'"unrelated_observations"\s*:\s*\[(.*?)\]', clean_text, re.DOTALL
        )
        if unrelated_match:
            unrelated_raw = unrelated_match.group(1)
            for obs in re.findall(r'"([^"\\]*(?:\\.[^"\\]*)*)"', unrelated_raw):
                extracted_unrelated.append(obs.replace(r"\"", '"').strip())

        return {
            "root_cause": (
                extracted_cause or "Automated diagnosis identified deployment failure."
            ),
            "causal_remediation": extracted_actions,
            "unrelated_observations": extracted_unrelated,
            "remediation_actions": extracted_actions,
            "confidence": 0.90,
        }
    except Exception as exc:
        logger.warning("Vertex AI Gemini synthesis call failed: %s", exc)
        return None
    finally:
        if owns_client:
            c.close()
