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

    prompt = (
        "You are an automated Site Reliability Engineering diagnostic assistant.\n"
        "Analyze the following failed GitHub Actions CI/CD pipeline step and raw runner logs:\n\n"
        f"Job: {job_name}\n"
        f"Step: {step_name}\n"
        f"Runner Logs:\n```\n{log_excerpt}\n```\n\n"
        "Instructions:\n"
        "1. Identify the exact root cause grounded strictly in the provided runner log text.\n"
        "2. Do NOT invent or hallucinate revision numbers, resource names, or "
        "projects not in the logs.\n"
        "3. Provide actionable remediation steps to fix the root cause.\n"
        "4. Respond with valid JSON with keys: 'root_cause' (string), "
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
            "maxOutputTokens": 1024,
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
                    "root_cause": str(parsed["root_cause"]),
                    "remediation_actions": [str(a) for a in parsed.get("remediation_actions", [])],
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
                            "root_cause": str(parsed["root_cause"]),
                            "remediation_actions": [
                                str(a) for a in parsed.get("remediation_actions", [])
                            ],
                            "confidence": float(parsed.get("confidence", 0.95)),
                        }
                except Exception:
                    pass

        return {
            "root_cause": clean_text,
            "remediation_actions": [],
            "confidence": 0.90,
        }
    except Exception as exc:
        logger.warning("Vertex AI Gemini synthesis call failed: %s", exc)
        return None
    finally:
        if owns_client:
            c.close()
