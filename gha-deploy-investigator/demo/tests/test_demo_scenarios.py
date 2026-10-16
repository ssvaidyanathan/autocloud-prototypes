import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from agent.app.config import AgentConfig
from agent.app.contracts.incident import Incident
from agent.app.dispatcher import classify_stage
from agent.app.specialists.pipeline_logs import PipelineLogsSpecialist
from agent.app.specialists.runtime_health import RuntimeHealthSpecialist
from agent.app.tools.runtime_tools import reset_runtime_state, set_mock_revision

VENV_BIN = Path(sys.executable).parent
RUFF_BIN = str(VENV_BIN / "ruff") if (VENV_BIN / "ruff").exists() else "ruff"
PYTEST_BIN = str(VENV_BIN / "pytest") if (VENV_BIN / "pytest").exists() else "pytest"


def test_pristine_demo_app_functionality(demo_client: TestClient) -> None:
    """Verify sample pipeline app root, health, and order API endpoints."""
    # 1. Health probe
    health_resp = demo_client.get("/health")
    assert health_resp.status_code == 200
    assert health_resp.json()["status"] == "healthy"

    # 2. Root metadata
    root_resp = demo_client.get("/")
    assert root_resp.status_code == 200
    assert root_resp.json()["service"] == "sample-pipeline-app"

    # 3. List orders
    list_resp = demo_client.get("/api/v1/orders")
    assert list_resp.status_code == 200
    assert len(list_resp.json()) >= 1

    # 4. Create and retrieve order
    create_resp = demo_client.post(
        "/api/v1/orders",
        json={
            "customer_id": "cust-demo",
            "customer_email": "demo@example.com",
            "items": [
                {
                    "item_id": "sku-demo-1",
                    "name": "Demo Widget",
                    "quantity": 2,
                    "unit_price": 10.0,
                }
            ],
        },
    )
    assert create_resp.status_code == 201
    created_id = create_resp.json()["order_id"]
    get_resp = demo_client.get(f"/api/v1/orders/{created_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["order_id"] == created_id


def test_scenario_1_lint_failure_lifecycle(demo_app_path: Path) -> None:
    """Verify Scenario 1 injection, ruff failure, specialist diagnosis, and clean reset."""
    scenarios_dir = demo_app_path / "scenarios"
    main_py = demo_app_path / "app" / "main.py"

    try:
        # 1. Trigger lint fault
        res = subprocess.run(
            ["bash", str(scenarios_dir / "trigger-lint-failure.sh")],
            capture_output=True,
            text=True,
            check=True,
        )
        assert "Injected lint violations" in res.stdout

        # Verify ruff fails on app/main.py
        ruff_res = subprocess.run(
            [RUFF_BIN, "check", str(main_py)],
            capture_output=True,
            text=True,
        )
        assert ruff_res.returncode != 0
        assert "F401" in ruff_res.stdout or "E701" in ruff_res.stdout

        # 2. Verify PipelineLogsSpecialist diagnoses linter failure
        specialist = PipelineLogsSpecialist()
        incident = Incident(
            id="inc-demo-lint",
            source="github",
            external_id="101",
            external_key="repo#101",
            title="CI Failure: Lint",
            description="Workflow failed on branch main at step Run Ruff Lint",
            opened_at=datetime.now(timezone.utc),
            severity="P3",
            github_repo="owner/sample-pipeline-app",
            github_run_id=101,
        )
        failing_job = {"id": 1, "name": "Code Quality & Lint"}
        failing_step = {"number": 3, "name": "Run Ruff Lint"}
        diag = specialist.diagnose(
            incident=incident,
            failing_job=failing_job,
            failing_step=failing_step,
            log_text=ruff_res.stdout,
        )
        assert diag.stage == "lint"
        assert "main.py" in diag.probable_cause
        assert len(diag.annotations) > 0

    finally:
        # 3. Reset scenario
        subprocess.run(
            ["bash", str(scenarios_dir / "reset-scenarios.sh")],
            capture_output=True,
            text=True,
            check=True,
        )
        # Verify clean state
        clean_check = subprocess.run(
            [RUFF_BIN, "check", str(main_py)],
            capture_output=True,
            text=True,
        )
        assert clean_check.returncode == 0


def test_scenario_2_test_failure_lifecycle(demo_app_path: Path) -> None:
    """Verify Scenario 2 injection, pytest failure, specialist diagnosis, and clean reset."""
    scenarios_dir = demo_app_path / "scenarios"
    test_main_py = demo_app_path / "tests" / "test_main.py"

    try:
        # 1. Trigger test fault
        res = subprocess.run(
            ["bash", str(scenarios_dir / "trigger-test-failure.sh")],
            capture_output=True,
            text=True,
            check=True,
        )
        assert "Injected failing assertion" in res.stdout

        # Verify pytest fails
        pytest_res = subprocess.run(
            [PYTEST_BIN, str(test_main_py)],
            capture_output=True,
            text=True,
            cwd=str(demo_app_path),
        )
        assert pytest_res.returncode != 0
        assert "FAILED" in pytest_res.stdout or "AssertionError" in pytest_res.stdout

        # 2. Verify specialist diagnosis
        specialist = PipelineLogsSpecialist()
        incident = Incident(
            id="inc-demo-test",
            source="github",
            external_id="102",
            external_key="repo#102",
            title="CI Failure: Test",
            description="Workflow failed on branch main at step Run Pytest",
            opened_at=datetime.now(timezone.utc),
            severity="P3",
            github_repo="owner/sample-pipeline-app",
            github_run_id=102,
        )
        failing_job = {"id": 2, "name": "Unit & Integration Tests"}
        failing_step = {"number": 3, "name": "Run Pytest"}
        diag = specialist.diagnose(
            incident=incident,
            failing_job=failing_job,
            failing_step=failing_step,
            log_text=pytest_res.stdout,
        )
        assert diag.stage == "test"
        assert "test_order_discount_calculation_failure" in diag.probable_cause

    finally:
        # 3. Reset scenario
        subprocess.run(
            ["bash", str(scenarios_dir / "reset-scenarios.sh")],
            capture_output=True,
            text=True,
            check=True,
        )
        clean_pytest = subprocess.run(
            [PYTEST_BIN, str(test_main_py)],
            capture_output=True,
            text=True,
            cwd=str(demo_app_path),
        )
        assert clean_pytest.returncode == 0


def test_scenario_3_build_failure_lifecycle(demo_app_path: Path) -> None:
    """Verify Scenario 3 injection, Dockerfile modification, specialist diagnosis, and reset."""
    scenarios_dir = demo_app_path / "scenarios"
    dockerfile = demo_app_path / "Dockerfile"

    try:
        # 1. Trigger build fault
        res = subprocess.run(
            ["bash", str(scenarios_dir / "trigger-build-failure.sh")],
            capture_output=True,
            text=True,
            check=True,
        )
        assert "Injected failing COPY instruction" in res.stdout
        dockerfile_content = dockerfile.read_text()
        assert "missing_asset_configuration.json" in dockerfile_content

        # 2. Verify specialist diagnoses simulated build log
        build_log = (
            "#7 [builder 3/4] COPY missing_asset_configuration.json .\n"
            "#7 ERROR: failed to solve: failed to compute cache key: "
            "COPY failed: file not found in build context: missing_asset_configuration.json\n"
        )
        specialist = PipelineLogsSpecialist()
        incident = Incident(
            id="inc-demo-build",
            source="github",
            external_id="103",
            external_key="repo#103",
            title="CI Failure: Container Build",
            description="Workflow failed on branch main at step Build Container Image",
            opened_at=datetime.now(timezone.utc),
            severity="P3",
            github_repo="owner/sample-pipeline-app",
            github_run_id=103,
        )
        failing_job = {"id": 3, "name": "Container Build & Package"}
        failing_step = {"number": 2, "name": "Build Container Image"}
        diag = specialist.diagnose(
            incident=incident,
            failing_job=failing_job,
            failing_step=failing_step,
            log_text=build_log,
        )
        assert diag.stage == "build"
        assert "COPY failed" in diag.probable_cause or "missing_asset" in diag.probable_cause

    finally:
        # 3. Reset scenario
        subprocess.run(
            ["bash", str(scenarios_dir / "reset-scenarios.sh")],
            capture_output=True,
            text=True,
            check=True,
        )
        assert "missing_asset_configuration.json" not in dockerfile.read_text()


def test_scenario_4_deploy_failure_lifecycle(demo_app_path: Path) -> None:
    """Verify Scenario 4 injection, deploy.sh simulation, specialist diagnosis, and reset."""
    scenarios_dir = demo_app_path / "scenarios"
    deploy_sh = demo_app_path / "scripts" / "deploy.sh"
    reset_runtime_state()

    try:
        # 1. Trigger deploy fault
        res = subprocess.run(
            ["bash", str(scenarios_dir / "trigger-deploy-failure.sh")],
            capture_output=True,
            text=True,
            check=True,
        )
        assert "Injected OOM fault parameters" in res.stdout
        deploy_content = deploy_sh.read_text()
        assert "DEMO_ALLOCATE_OOM=true" in deploy_content

        # Execute deploy.sh simulation
        sim_res = subprocess.run(
            ["bash", str(deploy_sh)],
            capture_output=True,
            text=True,
            env={**os.environ, "CI_SIMULATION": "true"},
        )
        assert sim_res.returncode == 1
        assert "exit code 137" in sim_res.stderr

        # 2. Configure mock runtime revision and verify RuntimeHealthSpecialist
        set_mock_revision(
            "sample-pipeline-app-00004-oom",
            {
                "service_name": "sample-pipeline-app",
                "revision_name": "sample-pipeline-app-00004-oom",
                "region": "us-central1",
                "conditions": [
                    {
                        "type": "Ready",
                        "status": "False",
                        "reason": "ContainerTerminated",
                        "message": "Container terminated due to memory limit (exit code 137)",
                    }
                ],
                "container_statuses": [
                    {
                        "name": "sample-pipeline-app",
                        "restart_count": 3,
                        "last_exit_code": 137,
                    }
                ],
            },
        )
        specialist = RuntimeHealthSpecialist()
        incident = Incident(
            id="inc-demo-deploy",
            source="github",
            external_id="104",
            external_key="repo#104",
            title="CI Failure: Deploy to Cloud Run",
            description="Workflow failed on branch main at step Deploy to Cloud Run",
            opened_at=datetime.now(timezone.utc),
            severity="P2",
            github_repo="owner/sample-pipeline-app",
            github_run_id=104,
        )
        failing_job = {"id": 4, "name": "Deploy to Cloud Run"}
        failing_step = {"number": 2, "name": "Deploy to Cloud Run"}
        diag = specialist.diagnose(
            incident=incident,
            failing_job=failing_job,
            failing_step=failing_step,
            log_text=sim_res.stderr,
            config=AgentConfig(app_env="local"),
        )
        assert diag.stage == "deploy"
        assert "137" in diag.probable_cause or "OOM" in diag.probable_cause
        assert any("--memory 1Gi" in act for act in diag.remediation_actions)

    finally:
        # 3. Reset scenario
        subprocess.run(
            ["bash", str(scenarios_dir / "reset-scenarios.sh")],
            capture_output=True,
            text=True,
            check=True,
        )
        clean_text = deploy_sh.read_text()
        assert 'MEMORY="${MEMORY:-512Mi}"' in clean_text
        assert 'MEMORY="${MEMORY:-128Mi}"' not in clean_text
        assert 'EXTRA_ENV_VARS="${EXTRA_ENV_VARS:-DEMO_ALLOCATE_OOM=true}"' not in clean_text


def test_stage_classification_for_demo_pipeline_jobs() -> None:
    """Verify that all 4 demo pipeline workflow jobs classify to correct stages."""
    assert classify_stage("Code Quality & Lint", "Run Ruff Lint") == "lint"
    assert classify_stage("Unit & Integration Tests", "Run Pytest") == "test"
    assert classify_stage("Container Build & Package", "Build Container Image") == "build"
    assert classify_stage("Deploy to Cloud Run", "Deploy to Cloud Run") == "deploy"


def test_seed_demo_repo_script(demo_app_path: Path) -> None:
    """Verify seed-demo-repo.sh creates a pristine standalone git repository."""
    seed_script = demo_app_path / "seed-demo-repo.sh"
    target_dir = Path(tempfile.mkdtemp(prefix="test-gdi-seed-"))

    try:
        # Execute seed script
        res = subprocess.run(
            ["bash", str(seed_script), str(target_dir)],
            capture_output=True,
            text=True,
            check=True,
        )
        assert "SUCCESS: Standalone demo repository seeded" in res.stdout

        # Verify git repository created
        git_verify = subprocess.run(
            ["git", "-C", str(target_dir), "status"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert "On branch main" in git_verify.stdout

        # Verify key pipeline files exist in seeded repository
        assert (target_dir / ".github" / "workflows" / "pipeline.yml").exists()
        assert (target_dir / "app" / "main.py").exists()
        assert (target_dir / "tests" / "test_main.py").exists()
        assert (target_dir / "Dockerfile").exists()
        assert (target_dir / "scenarios" / "trigger-lint-failure.sh").exists()
        assert (target_dir / "scenarios" / "reset-scenarios.sh").exists()

        # Verify no backup or temporary files leak into the seeded repo
        assert len(list(target_dir.glob("**/*.bak"))) == 0

    finally:
        shutil.rmtree(target_dir, ignore_errors=True)
