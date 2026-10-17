"""Hermetic unit tests for multi-turn ADK Runner pattern across specialists."""

from datetime import datetime, timezone
from typing import Any

from agent.app.contracts.incident import Incident
from agent.app.specialists.iac_config import IaCConfigSpecialist
from agent.app.specialists.pipeline_logs import PipelineLogsSpecialist
from agent.app.specialists.runtime_health import RuntimeHealthSpecialist
from agent.app.tools.github_tools import reset_github_mock_state, set_mock_diff
from agent.app.tools.runtime_tools import reset_runtime_state
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types


def _make_incident(failing_stage: str = "pipeline") -> Incident:
    return Incident(
        id="gha-test-agentic-01",
        source="github",
        external_id="555123",
        external_key="my-org/sample-app#555123",
        external_url="https://github.com/my-org/sample-app/actions/runs/555123",
        title="[CI/CD Failure] sample-app: deploy-cloudrun",
        description="Workflow run 555123 failed",
        severity="P2",
        service_hint="sample-app",
        opened_at=datetime.now(timezone.utc),
        github_repo="my-org/sample-app",
        github_run_id=555123,
        github_head_sha="a1b2c3d4e5f6",
        github_branch="main",
        github_failing_stage=failing_stage,
    )


class PipelineLogsSuccessLlm(BaseLlm):
    """Stubbed LLM simulating a successful 3-turn drill-down pass for PipelineLogsSpecialist."""

    model: str = "stub"
    _turn: int = 0

    def __init__(self) -> None:
        super().__init__(model="stub")
        self._turn = 0

    async def generate_content_async(self, llm_request: Any, stream: bool = False) -> Any:
        self._turn += 1
        if self._turn == 1:
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part.from_function_call(
                            name="load_skill",
                            args={"skill_name": "lint-troubleshooting"},
                        )
                    ],
                )
            )
        elif self._turn == 2:
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part.from_function_call(
                            name="get_commit_diff",
                            args={
                                "repo": "my-org/sample-app",
                                "head_sha": "a1b2c3d4e5f6",
                            },
                        )
                    ],
                )
            )
        else:
            json_text = (
                '{"summary": "PipelineLogs agentic pass identified linter errors in app/main.py.", '
                '"confidence": 0.95, '
                '"anomalies": ["Unused import in app/main.py"], '
                '"evidence": [{"kind": "git_diff", '
                '"excerpt": "Commit modified app/main.py and pyproject.toml", '
                '"timestamp": "2026-10-17T08:00:00Z"}]}'
            )
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[types.Part(text=json_text)],
                )
            )


class RuntimeHealthSuccessLlm(BaseLlm):
    """Stubbed LLM simulating a successful 3-turn drill-down pass for RuntimeHealthSpecialist."""

    model: str = "stub"
    _turn: int = 0

    def __init__(self) -> None:
        super().__init__(model="stub")
        self._turn = 0

    async def generate_content_async(self, llm_request: Any, stream: bool = False) -> Any:
        self._turn += 1
        if self._turn == 1:
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part.from_function_call(
                            name="load_skill",
                            args={"skill_name": "cloudrun-deploy-troubleshooting"},
                        )
                    ],
                )
            )
        elif self._turn == 2:
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part.from_function_call(
                            name="describe_service",
                            args={
                                "service_name": "sample-app",
                                "region": "us-central1",
                            },
                        )
                    ],
                )
            )
        else:
            json_text = (
                '{"summary": "RuntimeHealth agentic pass verified Cloud Run service status.", '
                '"confidence": 0.90, '
                '"anomalies": ["Service healthy but container failed deployment probe"], '
                '"evidence": [{"kind": "cloud_run_service", '
                '"excerpt": "Service sample-app Ready in us-central1", '
                '"timestamp": "2026-10-17T08:00:00Z"}]}'
            )
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[types.Part(text=json_text)],
                )
            )


class IaCConfigSuccessLlm(BaseLlm):
    """Stubbed LLM simulating a successful 3-turn drill-down pass for IaCConfigSpecialist."""

    model: str = "stub"
    _turn: int = 0

    def __init__(self) -> None:
        super().__init__(model="stub")
        self._turn = 0

    async def generate_content_async(self, llm_request: Any, stream: bool = False) -> Any:
        self._turn += 1
        if self._turn == 1:
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part.from_function_call(
                            name="load_skill",
                            args={"skill_name": "cloudrun-deploy-troubleshooting"},
                        )
                    ],
                )
            )
        elif self._turn == 2:
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part.from_function_call(
                            name="get_commit_diff",
                            args={
                                "repo": "my-org/sample-app",
                                "head_sha": "a1b2c3d4e5f6",
                            },
                        )
                    ],
                )
            )
        else:
            json_text = (
                '{"summary": "IaCConfig agentic pass verified Terraform manifest error.", '
                '"confidence": 0.88, '
                '"anomalies": ["Syntax error on main.tf line 45"], '
                '"evidence": [{"kind": "git_diff", '
                '"excerpt": "Commit modified main.tf", '
                '"timestamp": "2026-10-17T08:00:00Z"}]}'
            )
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[types.Part(text=json_text)],
                )
            )


def test_pipeline_logs_specialist_multi_turn_runner() -> None:
    """Verify PipelineLogsSpecialist executes 3 turns with ADK Runner using stubbed model."""
    reset_github_mock_state()
    set_mock_diff(
        "a1b2c3d4e5f6",
        {"files": [{"filename": "app/main.py"}, {"filename": "pyproject.toml"}]},
    )

    specialist = PipelineLogsSpecialist()
    specialist.llm_agent.model = PipelineLogsSuccessLlm()

    incident = _make_incident("lint")
    job = {"id": 101, "name": "lint-job"}
    step = {"name": "Run Ruff Linter", "conclusion": "failure"}
    log_text = "app/main.py:42:5: F841 local variable unused"

    res = specialist.diagnose(incident, job, step, log_text)
    assert res.stage == "lint"
    assert "PipelineLogs agentic pass identified linter errors" in res.summary
    assert res.skill_first is True
    assert "lint-troubleshooting" in res.skills_used
    assert len(res.queries_run) >= 1
    assert any("get_commit_diff" in q for q in res.queries_run)
    assert any("main.py" in e.excerpt for e in res.evidence_items)


def test_runtime_health_specialist_multi_turn_runner() -> None:
    """Verify RuntimeHealthSpecialist executes 3 turns with ADK Runner using stubbed model."""
    reset_runtime_state()
    specialist = RuntimeHealthSpecialist()
    specialist.llm_agent.model = RuntimeHealthSuccessLlm()

    incident = _make_incident("deploy")
    job = {"id": 102, "name": "deploy-cloudrun"}
    step = {"name": "Deploy to Cloud Run", "conclusion": "failure"}
    log_text = "Deploying container to Cloud Run service [sample-app] in region [us-central1]..."

    res = specialist.diagnose(incident, job, step, log_text)
    assert res.stage == "deploy"
    assert "RuntimeHealth agentic pass verified Cloud Run service status" in res.summary
    assert res.skill_first is True
    assert "cloudrun-deploy-troubleshooting" in res.skills_used
    assert len(res.queries_run) >= 1
    assert any("describe_service" in q for q in res.queries_run)


def test_iac_config_specialist_multi_turn_runner() -> None:
    """Verify IaCConfigSpecialist executes 3 turns with ADK Runner using stubbed model."""
    reset_github_mock_state()
    set_mock_diff("a1b2c3d4e5f6", {"files": [{"filename": "main.tf"}]})

    specialist = IaCConfigSpecialist()
    specialist.llm_agent.model = IaCConfigSuccessLlm()

    incident = _make_incident("iac_config")
    job = {"id": 103, "name": "terraform-apply"}
    step = {"name": "Terraform Apply", "conclusion": "failure"}
    log_text = "Error: Unsupported attribute on main.tf line 45"

    res = specialist.diagnose(incident, job, step, log_text)
    assert res.stage == "iac_config"
    assert "IaCConfig agentic pass verified Terraform manifest error" in res.summary
    assert res.skill_first is True
    assert "cloudrun-deploy-troubleshooting" in res.skills_used
    assert len(res.queries_run) >= 1
    assert any("get_commit_diff" in q for q in res.queries_run)
