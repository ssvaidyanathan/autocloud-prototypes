"""The CI workflow covers every Terraform root and runs the deploy-script suites on macOS bash 3.2
(sec L8; test review: macOS job coverage)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT.parent / ".github" / "workflows" / "alert-triage-agent-ci.yml"
TF_DIR = ROOT / "infra" / "terraform"


def _text() -> str:
    return WORKFLOW.read_text()


def test_terraform_job_validates_every_root():
    loop = re.search(r"for stack in ([a-z -]+); do", _text())
    assert loop, "terraform validate loop not found"
    validated = set(loop.group(1).split())
    roots = {p.parent.name for p in TF_DIR.glob("*/versions.tf")} | {p.parent.name for p in TF_DIR.glob("*/main.tf")}
    assert "triage-iam" in roots
    assert roots <= validated, f"not validated in CI: {sorted(roots - validated)}"


def test_macos_job_runs_the_deploy_script_suites():
    text = _text()
    job = text[text.index("scripts-macos:"):text.index("shellcheck:")]
    for suite in ("test_bash32_compat.py", "test_macos_userland.py", "test_fresh_deploy_scripts.py",
                  "test_apigee_prefix.py", "test_deploy_scripts_env_vars.py"):
        assert f"tests/{suite}" in job, suite
