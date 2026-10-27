"""Drift guards between the applications Terraform stack and the scripts that
mutate the same Cloud Run services with gcloud.

The scenario scripts update ata-inventory-api in place (``--update-env-vars``).
Every variable they write must also be declared in Terraform. Otherwise the
next applications apply (including one targeted at orders, which depends on
inventory's URI) creates a new inventory revision that drops the variable.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
APPS_MAIN_TF = REPO_ROOT / "infra/terraform/applications/main.tf"

# Scripts that update the Terraform-managed inventory service in place.
INVENTORY_UPDATE_SCRIPTS = [
    "scripts/scenarios/s5_bad_deploy.sh",
    # restore_inventory_pinned_baseline, used by reset-scenarios.sh and the S5 revert.
    "scripts/scenarios/common.sh",
]


def _resource_block(tf_text: str, rtype: str, rname: str) -> str:
    start = tf_text.index(f'resource "{rtype}" "{rname}"')
    nxt = tf_text.find("\nresource ", start + 1)
    return tf_text[start:] if nxt == -1 else tf_text[start:nxt]


def _inventory_env() -> dict:
    block = _resource_block(APPS_MAIN_TF.read_text(), "google_cloud_run_v2_service", "inventory_api")
    pairs = re.findall(r'env\s*\{\s*name\s*=\s*"([^"]+)"\s*value\s*=\s*(.+)', block)
    return {name: value.strip() for name, value in pairs}


def _script_env_names(script: str) -> set:
    text = (REPO_ROOT / script).read_text()
    names = set()
    for assignments in re.findall(r'--update-env-vars="([^"]+)"', text):
        var = re.fullmatch(r"\$\{(\w+)\}", assignments)
        if var:
            # --update-env-vars="${VAR}": read VAR="..." from the same script.
            assignments = re.search(rf'^(?:export )?{var.group(1)}="([^"]+)"', text, re.M).group(1)
        names.update(item.split("=", 1)[0] for item in assignments.split(","))
    return names


@pytest.mark.parametrize("script", INVENTORY_UPDATE_SCRIPTS)
def test_inventory_terraform_declares_every_env_var_the_script_updates(script):
    names = _script_env_names(script)
    assert names, f"no --update-env-vars found in {script}; parser out of date?"

    missing = names - set(_inventory_env())

    assert not missing, f"{script} sets {sorted(missing)} on ata-inventory-api but main.tf does not declare them"


LEGACY_ALIASES = ("DB_USER", "MOCK_DB")

# Every file that writes ata-inventory-api's env (and the app that reads it).
ALIAS_FREE_FILES = [
    "infra/terraform/applications/main.tf",
    "scripts/scenarios/common.sh",
    "scripts/scenarios/s5_bad_deploy.sh",
    "scripts/reset-scenarios.sh",
    "scripts/repin-inventory.sh",
    "demo/ata-inventory-api/scripts/deploy.sh",
    "demo/ata-inventory-api/app/config.py",
]


def _baseline_env_names() -> set:
    text = (REPO_ROOT / "scripts/scenarios/common.sh").read_text()
    value = re.search(r'^INVENTORY_BASELINE_ENV_VARS="([^"]+)"', text, re.M).group(1)
    return {item.split("=", 1)[0] for item in value.split(",")}


def test_inventory_terraform_declares_no_legacy_env_alias():
    assert not set(LEGACY_ALIASES) & set(_inventory_env())


@pytest.mark.parametrize("path", ALIAS_FREE_FILES)
def test_no_legacy_alias_is_set_or_read(path):
    """Only ATA_INVENTORY_* names; the legacy aliases appear only where they are removed."""
    text = (REPO_ROOT / path).read_text()
    for alias in LEGACY_ALIASES:
        for m in re.finditer(rf"(?<![A-Z_]){alias}(?![A-Z_])", text):
            line = text[text.rfind("\n", 0, m.start()) + 1:text.find("\n", m.end())]
            allowed = (
                'INVENTORY_LEGACY_ENV_NAMES="DB_USER,MOCK_DB"' in line  # the removal list
                or line.lstrip().startswith("#")  # comments explaining the removal
                # inside a Python module docstring (config.py says the aliases are not read)
                or (path.endswith(".py") and text.startswith('"""') and m.start() < text.index('"""', 3))
            )
            assert allowed, f"{path}: legacy alias {alias} in: {line.strip()}"


def test_inventory_terraform_env_matches_the_scripts_baseline_exactly():
    """Terraform, deploy.sh, repin and the reset restore render the same env names."""
    assert set(_inventory_env()) == _baseline_env_names()


@pytest.mark.parametrize("rname", ["inventory_api", "orders_api"])
def test_demo_services_cap_instances_and_scale_to_zero(rname):
    block = _resource_block(APPS_MAIN_TF.read_text(), "google_cloud_run_v2_service", rname)
    scaling = re.search(r"scaling\s*\{([^}]*)\}", block).group(1)
    assert re.search(r"max_instance_count\s*=\s*var\.demo_max_instance_count", scaling)
    assert re.search(r"min_instance_count\s*=\s*0\b", scaling)


def test_applications_ignore_changes_cover_only_client_bookkeeping():
    """ignore_changes must not hide real config drift (image, env, template)."""
    lists = re.findall(r"ignore_changes\s*=\s*\[([^\]]*)\]", APPS_MAIN_TF.read_text())

    assert lists, "expected client/client_version to be ignored on the Cloud Run services"
    for raw in lists:
        # scaling = the service-level block (not template.scaling), which this config does not manage.
        assert {item.strip() for item in raw.split(",") if item.strip()} == {"client", "client_version", "scaling"}
        

def test_inventory_deploy_script_sets_exactly_the_complete_baseline_env():
    """deploy.sh replaces the env (--set-env-vars) with the complete baseline set, nothing else."""
    text = (REPO_ROOT / "demo/ata-inventory-api/scripts/deploy.sh").read_text()
    assert re.findall(r'--(?:set|update)-env-vars="([^"]+)"', text) == ["${INVENTORY_BASELINE_ENV_VARS}"]
    assert "--set-env-vars=" in text
