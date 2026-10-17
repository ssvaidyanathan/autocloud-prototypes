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

    prompt = (
        "You are an automated Site Reliability Engineering diagnostic assistant.\n"
        "Analyze the following failed GitHub Actions CI/CD pipeline step and raw runner logs:\n\n"
        f"Job: {job_name}\n"
        f"Step: {step_name}\n"
        f"{findings_context}\n"
        f"Runner Logs:\n```\n{log_excerpt}\n```\n\n"
        "Instructions:\n"
        "1. Identify the exact root cause grounded strictly in the provided runner log text and "
        "specialist findings.\n"
        "2. Incorporate the specialist's structured findings and exact error counts/codes. "
        "List each distinct error code with its count and message. "
        "Do NOT guess, approximate, or misattribute error counts, codes, or files. "
        "Cite all codes and counts accurately.\n"
        "3. Correlate findings with git diff hunks when available, explicitly distinguishing "
        "violations introduced directly by modified lines from consequential violations "
        "caused by line movements or cascading errors.\n"
        "4. Diff-grounded test failure remediation: When Git Diff Context indicates the failure "
        "was introduced in test code (e.g. failing assertion compares hardcoded literals or test "
        "was modified in diff without app changes), the primary remediation MUST lead directly "
        "with test code correction (fixing the test logic or correcting the assertion), NOT "
        "advise investigating application business logic when the diff proves the test itself "
        "is defective. Avoid hedging ('likely introduced by') when the diff is conclusive.\n"
        "5. Warning segregation: Put log warnings that are not causally linked to the failing step "
        "(for example, deprecation or runtime warnings not on the failing step's fatal error path) "
        "under 'Unrelated Observations' rather than primary remediation actions. Do not treat "
        "un-linked warnings as the primary root cause.\n"
        "6. Deploy & runtime failure correlation: Correlate container termination "
        "(e.g. exit 137 out-of-memory) with git diff changes (e.g. container resource limits, "
        "environment variables, or configuration changes) and quote the decisive container log "
        "lines before termination.\n"
        "7. Do NOT invent or hallucinate revision numbers, resource names, or "
        "projects not in the logs.\n"
        "8. Provide actionable remediation steps addressing each distinct violation directly.\n"
        "9. Respond with valid JSON with keys: 'root_cause' (string), "
        "'remediation_actions' (list of strings), 'confidence' (float between 0.0 and 1.0).\n"
    )

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
                return {
                    "root_cause": str(parsed["root_cause"]).strip(),
                    "remediation_actions": [
                        str(a).strip() for a in parsed.get("remediation_actions", [])
                    ],
                    "confidence": float(parsed.get("confidence", 0.95)),
                }
        except Exception:
            # Fallback: search for first { and last }
            m = re.search(r"(\{.*\})", clean_text, re.DOTALL)
            if m:
                try:
                    parsed = json.loads(m.group(1))
                    if isinstance(parsed, dict) and "root_cause" in parsed:
                        return {
                            "root_cause": str(parsed["root_cause"]).strip(),
                            "remediation_actions": [
                                str(a).strip() for a in parsed.get("remediation_actions", [])
                            ],
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
                r"\"?\s*,?\s*\"remediation_actions\".*$", "", cleaned_prose, flags=re.DOTALL
            )
            cleaned_prose = re.sub(r"\"?\s*\}\s*$", "", cleaned_prose)
            extracted_cause = cleaned_prose.strip(' \t\n\r"{}')

        extracted_actions = []
        rem_match = re.search(r'"remediation_actions"\s*:\s*\[(.*?)\]', clean_text, re.DOTALL)
        if rem_match:
            actions_raw = rem_match.group(1)
            for act in re.findall(r'"([^"\\]*(?:\\.[^"\\]*)*)"', actions_raw):
                extracted_actions.append(act.replace(r"\"", '"').strip())

        return {
            "root_cause": (
                extracted_cause or "Automated diagnosis identified deployment failure."
            ),
            "remediation_actions": extracted_actions,
            "confidence": 0.90,
        }
    except Exception as exc:
        logger.warning("Vertex AI Gemini synthesis call failed: %s", exc)
        return None
    finally:
        if owns_client:
            c.close()
