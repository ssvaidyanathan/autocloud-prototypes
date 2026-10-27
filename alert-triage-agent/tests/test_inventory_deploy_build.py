"""demo/ata-inventory-api/scripts/deploy.sh: build by default when the stack's pinned digest
is not in its Artifact Registry, or always for a fresh stack (no pin file); print the
deployed digest (fresh-deploy audit gap 16). Stubbed gcloud: nothing leaves this machine."""

import pytest

from tests.cloud_run_fake import NEW_DIGEST
from tests.test_scripts_stack_isolation import LIVE_ENV, PIN_DIGEST, PREFIX, ROOT, iso, recorded, run  # noqa: F401

DEPLOY = ROOT / "demo" / "ata-inventory-api" / "scripts" / "deploy.sh"
REPO = f"test-region1-docker.pkg.dev/test-project/{PREFIX}-docker/{PREFIX}-inventory-api"


def _gcloud(iso, verb):
    return [c for c in recorded(iso) if c[0] == "gcloud" and c[1:1 + len(verb)] == verb]


def _deployed_image(iso):
    (deploy,) = _gcloud(iso, ["run", "deploy"])
    return next(a.split("=", 1)[1] for a in deploy if a.startswith("--image="))


def test_pinned_digest_present_deploys_the_pin_without_building(iso):
    res = run(iso, [DEPLOY], ISO_AR="found", **LIVE_ENV)
    assert res.returncode == 0, res.stdout + res.stderr
    assert _gcloud(iso, ["builds", "submit"]) == []
    (probe,) = _gcloud(iso, ["artifacts", "docker", "images", "describe"])
    assert probe[5] == f"{REPO}@{PIN_DIGEST}"
    assert _deployed_image(iso) == f"{REPO}@{PIN_DIGEST}"
    assert f"IMAGE={REPO}@{PIN_DIGEST}" in res.stdout
    assert "write-pin" not in res.stdout  # deployed image is the pin


def test_pinned_digest_missing_builds_and_says_how_to_pin(iso):
    res = run(iso, [DEPLOY], ISO_AR="notfound", **LIVE_ENV)
    assert res.returncode == 0, res.stdout + res.stderr
    assert len(_gcloud(iso, ["builds", "submit"])) == 1
    assert "is not in" in res.stderr
    assert _deployed_image(iso) == f"{REPO}@{NEW_DIGEST}"
    assert f"IMAGE={REPO}@{NEW_DIGEST}" in res.stdout
    assert f"write-pin --revision {PREFIX}-inventory-api-00002-new --digest {NEW_DIGEST}" in res.stdout


def test_registry_error_fails_loudly_and_deploys_nothing(iso):
    res = run(iso, [DEPLOY], ISO_AR="error", **LIVE_ENV)
    assert res.returncode != 0
    assert "PERMISSION_DENIED" in res.stderr and "nothing deployed" in res.stderr
    assert _gcloud(iso, ["run", "deploy"]) == [] and _gcloud(iso, ["builds", "submit"]) == []


def test_build_false_with_missing_digest_fails_loudly(iso):
    res = run(iso, [DEPLOY], ISO_AR="notfound", BUILD_IMAGE="false", **LIVE_ENV)
    assert res.returncode != 0
    assert "BUILD_IMAGE=false" in res.stderr
    assert _gcloud(iso, ["run", "deploy"]) == [] and _gcloud(iso, ["builds", "submit"]) == []


def test_build_false_without_a_pin_fails_loudly(iso):
    (iso["tmp"] / f"inventory-pin.{PREFIX}.env").unlink()
    res = run(iso, [DEPLOY], BUILD_IMAGE="false", **LIVE_ENV)
    assert res.returncode != 0
    assert "has no pinned image" in res.stderr
    assert _gcloud(iso, ["run", "deploy"]) == []


def test_build_true_always_builds(iso):
    res = run(iso, [DEPLOY], ISO_AR="found", BUILD_IMAGE="true", **LIVE_ENV)
    assert res.returncode == 0, res.stdout + res.stderr
    assert len(_gcloud(iso, ["builds", "submit"])) == 1
    assert _gcloud(iso, ["artifacts", "docker", "images", "describe"]) == []


@pytest.mark.parametrize("value", ["yes", "", "TRUE"])
def test_invalid_build_image_value_fails(iso, value):
    res = run(iso, [DEPLOY], BUILD_IMAGE=value, **LIVE_ENV)
    if value == "":
        # Empty means unset: auto.
        assert res.returncode == 0, res.stdout + res.stderr
        return
    assert res.returncode != 0 and "must be auto, true or false" in res.stderr


def test_deploy_requires_the_stack_env(iso):
    res = run(iso, [DEPLOY], NAME_PREFIX="", **LIVE_ENV)
    assert res.returncode != 0 and "required environment not set: NAME_PREFIX" in res.stderr
    assert recorded(iso) == []


def test_deploy_sets_the_stack_label_and_baseline(iso):
    res = run(iso, [DEPLOY], ISO_AR="found", **LIVE_ENV)
    assert res.returncode == 0, res.stdout + res.stderr
    (deploy,) = _gcloud(iso, ["run", "deploy"])
    labels = next(a for a in deploy if a.startswith("--labels="))
    assert "app=alert-triage-agent" in labels and f"stack={PREFIX}" in labels
    assert f"--service-account={PREFIX}-inventory-sa@test-project.iam.gserviceaccount.com" in deploy
    assert "--max=1" in deploy and "--max-instances=1" in deploy and "--min-instances=0" in deploy
