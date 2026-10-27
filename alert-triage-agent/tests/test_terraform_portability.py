"""Terraform and bootstrap portability: a from-scratch deploy into any project/region/prefix.

Guards against the live stack's identifiers coming back into infra/terraform or the
bootstrap/deploy scripts, against hardcoded state backends, and against target
variables (project_id, region) regaining defaults.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TF_DIR = REPO_ROOT / "infra" / "terraform"
SCRIPTS = REPO_ROOT / "scripts"
BOOTSTRAP = SCRIPTS / "bootstrap-tfstate.sh"
DEPLOY_DEMO_INFRA = SCRIPTS / "deploy-demo-infra.sh"

ROOTS = ["foundation", "database", "applications", "observability", "triage-iam"]

# Identifiers of the live demo project: project id, project number, Cloud Run URL
# hash and region. None may appear in Terraform (examples included) or in the
# two scripts that bootstrap and deploy it.
LIVE_LITERALS = re.compile(r"cloud-assist-fde-2|185099261133|4klvlqnmpa|us-west1")

BASH = shutil.which("bash")


def _tf_files():
    files = [p for p in TF_DIR.rglob("*") if p.is_file() and ".terraform" not in p.parts]
    # backend.hcl / terraform.tfvars are generated or operator-local and gitignored.
    return [p for p in files if p.name not in ("backend.hcl", "terraform.tfvars", ".terraform.lock.hcl")]


def _variable_block(text: str, name: str) -> str:
    start = text.index(f'variable "{name}"')
    depth, i = 0, text.index("{", start)
    while True:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
        i += 1


def test_every_root_dir_is_known():
    found = sorted(p.name for p in TF_DIR.iterdir() if p.is_dir() and (p / "versions.tf").is_file())
    assert found == sorted(ROOTS)


@pytest.mark.parametrize("path", _tf_files(), ids=lambda p: str(p.relative_to(TF_DIR)))
def test_no_live_identifiers_in_terraform(path):
    hits = [f"{n}: {line.strip()}" for n, line in enumerate(path.read_text().splitlines(), 1)
            if LIVE_LITERALS.search(line)]
    assert not hits, f"live-project literal in {path}: {hits}"


@pytest.mark.parametrize("script", [BOOTSTRAP, DEPLOY_DEMO_INFRA], ids=lambda p: p.name)
def test_no_live_identifiers_in_owned_scripts(script):
    hits = [f"{n}: {line.strip()}" for n, line in enumerate(script.read_text().splitlines(), 1)
            if LIVE_LITERALS.search(line)]
    assert not hits, f"live-project literal in {script.name}: {hits}"


@pytest.mark.parametrize("path", _tf_files(), ids=lambda p: str(p.relative_to(TF_DIR)))
def test_no_bucket_literal_in_terraform(path):
    """State bucket names come from backend.hcl (partial backend config), never from a literal."""
    hits = re.findall(r'^\s*bucket\s*=\s*"[^"]*"', path.read_text(), re.M)
    assert not hits, f"bucket literal in {path}: {hits}"


@pytest.mark.parametrize("root", ROOTS)
def test_root_uses_partial_gcs_backend_and_aligned_versions(root):
    versions = (TF_DIR / root / "versions.tf").read_text()
    assert re.search(r'backend\s+"gcs"\s*\{\s*\}', versions), f"{root}: backend must be `backend \"gcs\" {{}}`"
    assert 'required_version = ">= 1.9.0"' in versions
    assert re.search(r'source\s*=\s*"hashicorp/google"\s*\n\s*version\s*=\s*"~> 6\.0"', versions)


@pytest.mark.parametrize("root", ROOTS)
def test_target_variables_have_no_default_and_prefix_is_validated(root):
    variables = (TF_DIR / root / "variables.tf").read_text()
    for name in ("project_id", "region"):
        assert not re.search(r"^\s*default\s*=", _variable_block(variables, name), re.M), \
            f"{root}: {name} must have no default"
    prefix = _variable_block(variables, "name_prefix")
    assert 'default     = "ata"' in prefix
    assert 'regex("^[a-z][a-z0-9]{1,9}$", var.name_prefix)' in prefix


def test_applications_images_are_required():
    variables = (TF_DIR / "applications" / "variables.tf").read_text()
    for name in ("inventory_image", "orders_image"):
        assert not re.search(r"^\s*default\s*=", _variable_block(variables, name), re.M), name


def test_examples_use_placeholders():
    for root in ROOTS:
        example = (TF_DIR / root / "terraform.tfvars.example").read_text()
        assert 'project_id' in example and '"your-project-id"' in example, root
        assert '"your-region"' in example, root


def test_webhook_channel_uses_deterministic_bridge_url():
    main_tf = (TF_DIR / "foundation" / "main.tf").read_text()
    assert 'data "google_project" "this"' in main_tf
    assert ('url      = "https://${var.name_prefix}-bridge-${data.google_project.this.number}'
            '.${var.region}.run.app/webhook/monitoring"') in main_tf


def test_foundation_enables_apis_and_creates_all_runtime_sas():
    main_tf = (TF_DIR / "foundation" / "main.tf").read_text()
    assert 'resource "google_project_service" "required"' in main_tf
    assert "disable_on_destroy = false" in main_tf
    for api in ("run", "cloudbuild", "artifactregistry", "cloudtasks", "secretmanager", "sqladmin",
                "monitoring", "logging", "storage", "iam", "iamcredentials", "cloudresourcemanager",
                "aiplatform", "apigee"):
        assert f'"{api}.googleapis.com"' in main_tf, api
    for sa in ("bridge", "agent", "inventory", "orders", "apigee", "build"):
        assert f'account_id   = "${{var.name_prefix}}-{sa}-sa"' in main_tf, sa
    # No tools SA: tools runs as the agent SA.
    assert "-tools-sa" not in main_tf
    for role in ("roles/aiplatform.user", "roles/apigee.readOnlyAdmin", "roles/cloudsql.client",
                 "roles/cloudsql.instanceUser"):
        assert role in main_tf


def test_bridge_and_agent_grants_are_scoped_to_the_stack():
    """No project-wide secret, bucket, invoker or act-as grants to the triage SAs."""
    main_tf = (TF_DIR / "foundation" / "main.tf").read_text()
    blocks = re.findall(r'resource "google_project_iam_member" "[^"]+" \{.*?\n\}', main_tf, re.S)
    for block in blocks:
        if "bridge_sa.email" in block or "agent_sa.email" in block:
            for role in ("secretmanager.secretAccessor", "storage.objectUser", "run.invoker",
                         "iam.serviceAccountUser", "cloudtasks.enqueuer"):
                assert role not in block, f"project-level {role} for a triage SA:\n{block}"
    assert 'resource "google_secret_manager_secret_iam_member" "bridge_secret_accessor"' in main_tf
    assert 'resource "google_storage_bucket_iam_member" "bridge_incidents_writer"' in main_tf
    assert 'resource "google_cloud_tasks_queue_iam_member" "bridge_triage_enqueuer"' in main_tf
    assert 'resource "google_service_account_iam_member" "bridge_sa_service_account_user"' in main_tf


def test_stack_label_everywhere_labels_are_set():
    foundation = (TF_DIR / "foundation" / "main.tf").read_text()
    assert re.search(r"common_labels = \{[^}]*stack\s+= var\.name_prefix", foundation, re.S)
    apps = (TF_DIR / "applications" / "main.tf").read_text()
    assert len(re.findall(r"stack\s+= var\.name_prefix", apps)) == 2
    obs = (TF_DIR / "observability" / "main.tf").read_text()
    assert len(re.findall(r'stack\s+= "\$\{var\.name_prefix\}"', obs)) == 8
    db = (TF_DIR / "database" / "main.tf").read_text()
    assert re.search(r"stack\s+= var\.name_prefix", db)


def test_applications_does_not_look_up_service_accounts():
    apps = (TF_DIR / "applications" / "main.tf").read_text()
    assert 'data "google_service_account"' not in apps


def test_schema_grants_the_iam_user():
    schema = (REPO_ROOT / "demo" / "db" / "schema.sql").read_text()
    assert "\\set ON_ERROR_STOP on" in schema
    assert 'GRANT SELECT, UPDATE ON inventory_items TO :"iam_user";' in schema
    db = (TF_DIR / "database" / "main.tf").read_text()
    assert 'type     = "CLOUD_IAM_SERVICE_ACCOUNT"' in db


# ---------------------------------------------------------------------------
# Scripts
# ---------------------------------------------------------------------------

def _copy_repo(tmp_path):
    """Scripts plus empty root dirs, so backend.hcl files land in tmp_path."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    for script in (BOOTSTRAP, DEPLOY_DEMO_INFRA):
        shutil.copy2(script, repo / "scripts" / script.name)
    for root in ROOTS:
        (repo / "infra" / "terraform" / root).mkdir(parents=True)
    return repo


def _clean_env(**extra):
    env = {k: v for k, v in os.environ.items()
           if k not in ("PROJECT_ID", "REGION", "NAME_PREFIX", "GCP_PROJECT", "GCP_REGION",
                        "INVENTORY_IMAGE", "ORDERS_IMAGE", "TFSTATE_BUCKET", "ENVIRONMENT")}
    env.update(extra)
    return env


def _run(cmd, env):
    return subprocess.run([BASH, *map(str, cmd)], env=env, capture_output=True, text=True, timeout=60, check=False)


def test_bootstrap_requires_project_region_and_prefix(tmp_path):
    repo = _copy_repo(tmp_path)
    res = _run([repo / "scripts" / BOOTSTRAP.name, "--dry-run"], _clean_env())
    assert res.returncode == 1
    for name in ("PROJECT_ID", "REGION", "NAME_PREFIX"):
        assert name in res.stderr


def test_bootstrap_rejects_an_invalid_prefix(tmp_path):
    repo = _copy_repo(tmp_path)
    res = _run([repo / "scripts" / BOOTSTRAP.name, "-p", "p", "-r", "r", "-n", "Bad_Prefix", "--dry-run"], _clean_env())
    assert res.returncode == 1
    assert "NAME_PREFIX" in res.stderr


def test_bootstrap_writes_backend_for_every_root_with_live_compatible_prefixes(tmp_path):
    """For NAME_PREFIX=ata the state locations stay "<project>-ata-tfstate" / "ata/<root>"."""
    repo = _copy_repo(tmp_path)
    res = _run([repo / "scripts" / BOOTSTRAP.name, "--write-backend-only"],
               _clean_env(PROJECT_ID="example-proj", REGION="us-central1", NAME_PREFIX="ata"))
    assert res.returncode == 0, res.stderr
    for root in ROOTS:
        hcl = (repo / "infra" / "terraform" / root / "backend.hcl").read_text()
        assert hcl == f'bucket = "example-proj-ata-tfstate"\nprefix = "ata/{root}"\n'


def test_bootstrap_enables_the_bootstrap_apis(tmp_path):
    repo = _copy_repo(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    gcloud = bin_dir / "gcloud"
    gcloud.write_text('#!/bin/sh\necho "gcloud $*" >> "$CALLS"\n')
    gcloud.chmod(0o755)
    env = _clean_env(PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}", CALLS=str(calls))
    res = _run([repo / "scripts" / BOOTSTRAP.name, "-p", "fresh-example", "-r", "us-central1", "-n", "atat"], env)
    assert res.returncode == 0, res.stderr
    log = calls.read_text()
    enable = [line for line in log.splitlines() if line.startswith("gcloud services enable")]
    assert enable, log
    for api in ("serviceusage", "cloudresourcemanager", "storage", "iam"):
        assert f"{api}.googleapis.com" in enable[0]
    assert "--project=fresh-example" in enable[0]
    assert "gs://fresh-example-atat-tfstate" in log
    assert (repo / "infra/terraform/triage-iam/backend.hcl").read_text().endswith('prefix = "atat/triage-iam"\n')


def test_deploy_demo_infra_requires_its_inputs(tmp_path):
    repo = _copy_repo(tmp_path)
    res = _run([repo / "scripts" / DEPLOY_DEMO_INFRA.name], _clean_env())
    assert res.returncode == 1
    for name in ("PROJECT_ID", "REGION", "NAME_PREFIX", "INVENTORY_IMAGE", "ORDERS_IMAGE"):
        assert name in res.stderr


def test_deploy_demo_infra_reconfigures_backend_and_passes_vars(tmp_path):
    repo = _copy_repo(tmp_path)
    env_vars = dict(PROJECT_ID="fresh-example", REGION="us-central1", NAME_PREFIX="atat",
                    INVENTORY_IMAGE="img-inv", ORDERS_IMAGE="img-ord")
    assert _run([repo / "scripts" / BOOTSTRAP.name, "--write-backend-only"], _clean_env(**env_vars)).returncode == 0
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    for name in ("terraform",):
        stub = bin_dir / name
        stub.write_text('#!/bin/sh\necho "$(basename "$PWD") $*" >> "$CALLS"\n')
        stub.chmod(0o755)
    # The Apigee step is out of scope here; replace it with a recorder.
    apigee = repo / "scripts" / "deploy-apigee-proxies.sh"
    apigee.write_text('#!/bin/sh\necho "apigee $PROJECT_ID $NAME_PREFIX" >> "$CALLS"\n')
    apigee.chmod(0o755)
    env = _clean_env(PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}", CALLS=str(calls), **env_vars)
    res = _run([repo / "scripts" / DEPLOY_DEMO_INFRA.name], env)
    assert res.returncode == 0, res.stderr
    lines = calls.read_text().splitlines()
    for root in ("database", "applications", "observability"):
        init = [l for l in lines if l.startswith(f"{root} init")]
        apply = [l for l in lines if l.startswith(f"{root} apply")]
        assert init and "-reconfigure -backend-config=backend.hcl" in init[0], lines
        assert apply and all(v in apply[0] for v in (
            "-var project_id=fresh-example", "-var region=us-central1", "-var name_prefix=atat")), lines
    app_apply = [l for l in lines if l.startswith("applications apply")][0]
    assert "-var inventory_image=img-inv" in app_apply and "-var orders_image=img-ord" in app_apply
    assert "-var environment=demo" in app_apply
    assert "environment" not in [l for l in lines if l.startswith("observability apply")][0]
    assert lines[-1] == "apigee fresh-example atat"


def test_deploy_demo_infra_refuses_another_stacks_backend(tmp_path):
    repo = _copy_repo(tmp_path)
    assert _run([repo / "scripts" / BOOTSTRAP.name, "--write-backend-only"],
                _clean_env(PROJECT_ID="fresh-example", REGION="r", NAME_PREFIX="ata")).returncode == 0
    env = _clean_env(PROJECT_ID="fresh-example", REGION="r", NAME_PREFIX="atat",
                     INVENTORY_IMAGE="i", ORDERS_IMAGE="o")
    res = _run([repo / "scripts" / DEPLOY_DEMO_INFRA.name], env)
    assert res.returncode == 1
    assert "not for NAME_PREFIX=atat" in res.stderr
