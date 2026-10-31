"""Tests for scripts/bootstrap-tfstate.sh argument parsing and dry-run functionality."""

import os
import subprocess
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
BOOTSTRAP_SCRIPT = ROOT_DIR / "scripts" / "bootstrap-tfstate.sh"


def test_bootstrap_tfstate_help() -> None:
    """Verify --help flag exits 0 and prints usage information."""
    result = subprocess.run(
        [str(BOOTSTRAP_SCRIPT), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "Usage: " in result.stdout
    assert "--project-id" in result.stdout
    assert "--bucket" in result.stdout
    assert "--dry-run" in result.stdout


def test_bootstrap_tfstate_dry_run_defaults() -> None:
    """Verify default bucket naming and dry run execution."""
    result = subprocess.run(
        [
            str(BOOTSTRAP_SCRIPT),
            "--project-id",
            "example-proj",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "Bucket Name:  example-proj-isa-tfstate" in result.stdout
    assert "gs://example-proj-isa-tfstate" in result.stdout
    assert "prefix=isa" in result.stdout
    assert "[DRY RUN] Plan for bootstrapping GCS state bucket:" in result.stdout


def test_bootstrap_tfstate_dry_run_custom_prefix() -> None:
    """Verify custom name prefix overrides default bucket name and labels."""
    result = subprocess.run(
        [
            str(BOOTSTRAP_SCRIPT),
            "--project-id",
            "example-proj",
            "--name-prefix",
            "acme",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "Bucket Name:  example-proj-acme-tfstate" in result.stdout
    assert "prefix=acme" in result.stdout


def test_bootstrap_tfstate_dry_run_custom_bucket() -> None:
    """Verify explicit bucket argument overrides convention."""
    result = subprocess.run(
        [
            str(BOOTSTRAP_SCRIPT),
            "--project-id",
            "example-proj",
            "--bucket",
            "my-custom-state-bucket",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "Bucket Name:  my-custom-state-bucket" in result.stdout
    assert "gs://my-custom-state-bucket" in result.stdout


def test_bootstrap_tfstate_missing_project_id() -> None:
    """Verify missing project ID causes non-zero exit."""
    clean_env = {k: v for k, v in os.environ.items() if k not in ("PROJECT_ID", "GCP_PROJECT")}
    clean_env["PATH"] = os.environ.get("PATH", "")
    result = subprocess.run(
        [str(BOOTSTRAP_SCRIPT), "--dry-run"],
        capture_output=True,
        text=True,
        env=clean_env,
        check=False,
    )
    assert result.returncode != 0
    assert "Error: GCP Project ID is required" in result.stderr
