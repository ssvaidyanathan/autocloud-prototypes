import os
import subprocess
import sys
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


def test_scenario_1_lint_failure_lifecycle(isolated_demo_app: Path, scenarios_dir: Path) -> None:
    """Verify Scenario 1 injection, ruff failure, specialist diagnosis, and clean reset."""
    main_py = isolated_demo_app / "app" / "main.py"

    try:
        # 1. Trigger lint fault in isolated copy
        res = subprocess.run(
            ["bash", str(scenarios_dir / "trigger-lint-failure.sh"), str(isolated_demo_app)],
            capture_output=True,
            text=True,
            check=True,
        )
        assert "Added currency formatting" in res.stdout

        # Verify ruff fails on app/main.py
        ruff_res = subprocess.run(
            [RUFF_BIN, "check", str(main_py)],
            capture_output=True,
            text=True,
        )
        assert ruff_res.returncode != 0
        assert "F401" in ruff_res.stdout or "E702" in ruff_res.stdout

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
        # 3. Reset scenario in isolated copy
        subprocess.run(
            ["bash", str(scenarios_dir / "reset-scenarios.sh"), str(isolated_demo_app)],
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


def test_scenario_2_test_failure_lifecycle(isolated_demo_app: Path, scenarios_dir: Path) -> None:
    """Verify Scenario 2 injection, pytest failure, specialist diagnosis, and clean reset."""
    test_main_py = isolated_demo_app / "tests" / "test_main.py"

    try:
        # 1. Trigger test fault in isolated copy
        res = subprocess.run(
            ["bash", str(scenarios_dir / "trigger-test-failure.sh"), str(isolated_demo_app)],
            capture_output=True,
            text=True,
            check=True,
        )
        assert "test_customer_tier_discount_calculation" in res.stdout

        # Verify pytest fails
        pytest_res = subprocess.run(
            [PYTEST_BIN, str(test_main_py)],
            capture_output=True,
            text=True,
            cwd=str(isolated_demo_app),
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
        assert "test_customer_tier_discount_calculation" in diag.probable_cause

    finally:
        # 3. Reset scenario in isolated copy
        subprocess.run(
            ["bash", str(scenarios_dir / "reset-scenarios.sh"), str(isolated_demo_app)],
            capture_output=True,
            text=True,
            check=True,
        )
        clean_pytest = subprocess.run(
            [PYTEST_BIN, str(test_main_py)],
            capture_output=True,
            text=True,
            cwd=str(isolated_demo_app),
        )
        assert clean_pytest.returncode == 0


def test_scenario_3_build_failure_lifecycle(isolated_demo_app: Path, scenarios_dir: Path) -> None:
    """Verify Scenario 3 injection, Dockerfile modification, specialist diagnosis, and reset."""
    dockerfile = isolated_demo_app / "Dockerfile"

    try:
        # 1. Trigger build fault in isolated copy
        res = subprocess.run(
            ["bash", str(scenarios_dir / "trigger-build-failure.sh"), str(isolated_demo_app)],
            capture_output=True,
            text=True,
            check=True,
        )
        assert "Injected missing COPY asset instruction" in res.stdout
        dockerfile_content = dockerfile.read_text()
        assert "cache-warmup.json" in dockerfile_content

        # 2. Verify specialist diagnoses simulated build log
        build_log = (
            "#7 [builder 3/4] COPY config/cache-warmup.json /app/config/cache-warmup.json\n"
            "#7 ERROR: failed to solve: failed to compute cache key: "
            "COPY failed: file not found in build context: config/cache-warmup.json\n"
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
        assert "COPY failed" in diag.probable_cause or "cache-warmup" in diag.probable_cause

    finally:
        # 3. Reset scenario in isolated copy
        subprocess.run(
            ["bash", str(scenarios_dir / "reset-scenarios.sh"), str(isolated_demo_app)],
            capture_output=True,
            text=True,
            check=True,
        )
        assert "cache-warmup.json" not in dockerfile.read_text()


def test_scenario_4_deploy_failure_lifecycle(
    isolated_demo_app: Path, scenarios_dir: Path, tmp_path: Path
) -> None:
    """Verify Scenario 4 injection, deploy.sh execution, specialist diagnosis, and reset."""
    deploy_sh = isolated_demo_app / "scripts" / "deploy.sh"
    reset_runtime_state()

    mock_gcloud = tmp_path / "mock_gcloud"

    try:
        # Create mock gcloud tool simulating Cloud Run deployment
        mock_gcloud.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "echo 'Deploying revision sample-pipeline-app-00004-err...' >&2\n"
            "echo 'ERROR: (gcloud.run.deploy) Container failed to start and listen ' "
            "'on port 8080.' >&2\n"
            "echo \"Revision 'sample-pipeline-app-00004-err' failed to become ready: "
            "Container terminated due to memory limit (exit code 137: Out of Memory).\" >&2\n"
            "exit 1\n"
        )
        mock_gcloud.chmod(0o755)

        # 1. Trigger deploy fault in isolated copy
        res = subprocess.run(
            ["bash", str(scenarios_dir / "trigger-deploy-failure.sh"), str(isolated_demo_app)],
            capture_output=True,
            text=True,
            check=True,
        )
        assert "Injected production cache buffer parameters" in res.stdout
        deploy_content = deploy_sh.read_text()
        assert "CACHE_PREWARM_BUFFER_MB=2048" in deploy_content

        # Execute deploy.sh using mock gcloud
        sim_res = subprocess.run(
            ["bash", str(deploy_sh)],
            capture_output=True,
            text=True,
            env={**os.environ, "GCLOUD": str(mock_gcloud)},
        )
        assert sim_res.returncode == 1
        assert "exit code 137" in sim_res.stderr

        # 2. Configure mock runtime revision and verify RuntimeHealthSpecialist
        set_mock_revision(
            "sample-pipeline-app-00004-err",
            {
                "service_name": "sample-pipeline-app",
                "revision_name": "sample-pipeline-app-00004-err",
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
        # 3. Reset scenario in isolated copy
        subprocess.run(
            ["bash", str(scenarios_dir / "reset-scenarios.sh"), str(isolated_demo_app)],
            capture_output=True,
            text=True,
            check=True,
        )
        clean_text = deploy_sh.read_text()
        assert 'MEMORY="${MEMORY:-512Mi}"' in clean_text
        assert 'MEMORY="${MEMORY:-256Mi}"' not in clean_text
        assert "CACHE_PREWARM_BUFFER_MB=2048" not in clean_text


def test_stage_classification_for_demo_pipeline_jobs() -> None:
    """Verify that all 4 demo pipeline workflow jobs classify to correct stages."""
    assert classify_stage("Code Quality & Lint", "Run Ruff Lint") == "lint"
    assert classify_stage("Unit & Integration Tests", "Run Pytest") == "test"
    assert classify_stage("Container Build & Package", "Build Container Image") == "build"
    assert classify_stage("Deploy to Cloud Run", "Deploy to Cloud Run") == "deploy"


def test_seed_demo_repo_script(demo_app_path: Path, tmp_path: Path) -> None:
    """Verify seed-demo-repo.sh creates a pristine repo without leaked scenarios."""
    seed_script = demo_app_path.parent / "seed-demo-repo.sh"
    target_dir = tmp_path / "seeded-demo-repo"

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
    assert (target_dir / "app" / "config.py").exists()
    assert (target_dir / "tests" / "test_main.py").exists()
    assert (target_dir / "Dockerfile").exists()
    assert (target_dir / "scripts" / "deploy.sh").exists()
    assert (target_dir / ".gitignore").exists()

    # CRITICAL INVARIANT: scenarios and seed script MUST NOT leak into customer repo
    assert not (target_dir / "scenarios").exists()
    assert not (target_dir / "seed-demo-repo.sh").exists()

    # Verify no backup or temporary files leak into the seeded repo
    assert len(list(target_dir.glob("**/*.bak"))) == 0
    assert len(list(target_dir.glob("**/*.orig"))) == 0


def test_scenario_commit_and_revert_workflow(
    demo_app_path: Path, scenarios_dir: Path, tmp_path: Path
) -> None:
    """Verify scenarios run with --commit produce commits and reset reverts them."""
    seed_script = demo_app_path.parent / "seed-demo-repo.sh"
    target_dir = tmp_path / "committed-demo-repo"

    # Seed fresh git repository
    subprocess.run(
        ["bash", str(seed_script), str(target_dir)],
        capture_output=True,
        text=True,
        check=True,
    )

    # Trigger Scenario 4 with --commit
    res = subprocess.run(
        ["bash", str(scenarios_dir / "trigger-deploy-failure.sh"), str(target_dir), "--commit"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "Created git commit" in res.stdout

    # Verify git commit log shows realistic message
    log_res = subprocess.run(
        ["git", "-C", str(target_dir), "log", "-1", "--pretty=%s"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "feat(cache): pre-warm order history cache on startup" in log_res.stdout

    # Reset with --commit flag
    reset_res = subprocess.run(
        ["bash", str(scenarios_dir / "reset-scenarios.sh"), str(target_dir), "--commit"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "SUCCESS: Demo app restored to clean pristine state" in reset_res.stdout

    # Check git status is clean
    status_res = subprocess.run(
        ["git", "-C", str(target_dir), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert status_res.stdout.strip() == ""
