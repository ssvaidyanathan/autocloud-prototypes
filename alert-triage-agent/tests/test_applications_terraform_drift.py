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


@pytest.mark.parametrize(
    "alias, primary",
    [("DB_USER", "ATA_INVENTORY_DB_USER"), ("MOCK_DB", "ATA_INVENTORY_MOCK_DB")],
)
def test_inventory_legacy_env_alias_renders_same_expression_as_primary(alias, primary):
    env = _inventory_env()

    assert alias in env and primary in env
    assert env[alias] == env[primary]


def test_applications_ignore_changes_cover_only_client_bookkeeping():
    """ignore_changes must not hide real config drift (image, env, template)."""
    lists = re.findall(r"ignore_changes\s*=\s*\[([^\]]*)\]", APPS_MAIN_TF.read_text())

    assert lists, "expected client/client_version to be ignored on the Cloud Run services"
    for raw in lists:
        # scaling = the service-level block (not template.scaling), which this config does not manage.
        assert {item.strip() for item in raw.split(",") if item.strip()} == {"client", "client_version", "scaling"}
