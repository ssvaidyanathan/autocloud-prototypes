"""F4: check-result.sh --until bounds the window above (re-checking S1 after S3 ran)."""

from tests.test_reset_archive_gcloud_storage import (  # noqa: F401  (gcs is a fixture)
    BUCKET, SCRIPTS_DIR, gcs, incident_md, put, run_script,
)

S1 = incident_md("grp_20261028t000139z_ata-inventory-api", "'2026-10-28T00:10:00Z'",
                 "'2026-10-28T00:01:39Z'", number="INC0010344")
# S3's orders group, later in the same session; updated (timestamp) long after.
S3 = incident_md("grp_20261028t014207z_ata-orders-v1", "'2026-10-28T01:50:10Z'",
                 "'2026-10-28T01:42:07Z'", number="INC0010347", origin="ata-orders-v1")
NO_OPEN_TIME = incident_md("grp-nostamp", "'2026-10-28T00:05:00Z'", "''", number="INC0000009")


def check(tmp_path, gcs, *args):
    return run_script(tmp_path, gcs, [SCRIPTS_DIR / "scenarios" / "check-result.sh", *args])


def test_until_excludes_groups_opened_later(tmp_path, gcs):
    _, root = gcs
    put(root, "archive/incident-s1.md", S1)
    put(root, "archive/incident-s3.md", S3)
    res = check(tmp_path, gcs, "--since", "2026-10-27T23:52:48Z", "--until", "2026-10-28T00:30:00Z",
                "--scenario", "s1")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Filter Until: 2026-10-28T00:30:00Z" in res.stdout
    assert "Found 1 incident group(s) in GCS" in res.stdout
    assert "INC0010344" in res.stdout and "INC0010347" not in res.stdout
    assert "Group Count:      Expected 1 -> [MATCH]" in res.stdout
    assert "incident-s3.md: opened 2026-10-28T01:42:07Z (first alert started_at), after --until" in res.stderr


def test_without_until_the_later_group_is_counted(tmp_path, gcs):
    _, root = gcs
    put(root, "archive/incident-s1.md", S1)
    put(root, "archive/incident-s3.md", S3)
    res = check(tmp_path, gcs, "--since", "2026-10-27T23:52:48Z", "--scenario", "s1")
    assert "Found 2 incident group(s) in GCS" in res.stdout


def test_group_updated_after_until_keeps_its_opening_time(tmp_path, gcs):
    _, root = gcs
    put(root, "archive/incident-s1.md", S1)  # opened 00:01:39, updated 00:10:00
    res = check(tmp_path, gcs, "--until", "2026-10-28T00:05:00Z", "--scenario", "s1")
    assert "Found 1 incident group(s) in GCS" in res.stdout


def test_until_excludes_file_without_opening_time_with_warning(tmp_path, gcs):
    _, root = gcs
    put(root, "archive/incident-nostamp.md", NO_OPEN_TIME)
    res = check(tmp_path, gcs, "--until", "2026-10-28T00:30:00Z")
    assert "Found 0 incident group(s) in GCS" in res.stdout
    assert (f"[WARNING] gs://{BUCKET}/archive/incident-nostamp.md: opening time undetermined; "
            "excluded from --until window") in res.stderr


def test_invalid_until_and_until_before_since_are_errors(tmp_path, gcs):
    res = check(tmp_path, gcs, "--until", "later")
    assert res.returncode != 0 and "--until 'later' is not an ISO-8601 time" in res.stderr
    res = check(tmp_path, gcs, "--since", "2026-10-28T01:00:00Z", "--until=2026-10-28T00:00:00Z")
    assert res.returncode != 0 and "is before --since" in res.stderr


def test_help_documents_until(tmp_path, gcs):
    res = check(tmp_path, gcs, "--help")
    assert "--until <time>" in res.stdout
