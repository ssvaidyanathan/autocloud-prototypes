"""
Unit tests for the ServiceNow Incident Cleanup Utility (repo-root/scripts/servicenow-cleanup.py).

Verifies:
- Encoded query construction for all filters (age, dates, numbers, caller, state, priority, etc.)
- Strict password and credential security (no --password flag, masked logs, getpass fallback)
- Dry-run mode makes zero DELETE requests and supports table, CSV, and JSON output
- Apply mode deletes exactly listed sys_ids
- Apply mode confirmation mismatch aborts cleanly without deleting
- Non-interactive apply with --yes executes deletions and echoes count
- Multi-page pagination and safety limit enforcement
- Client-side regex filtering on short descriptions
- Delete retry and exponential backoff on transient HTTP errors (429, 5xx)
- Failures reported accurately in summary
"""

from __future__ import annotations

import importlib.util
import io
import json
import logging
import os
import ssl
import sys
import types
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

# Dynamically import servicenow-cleanup.py (hyphenated script name)
SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "servicenow-cleanup.py"
spec = importlib.util.spec_from_file_location("servicenow_cleanup", SCRIPT_PATH)
if spec is None or spec.loader is None:
    raise ImportError(f"Cannot load spec from {SCRIPT_PATH}")
snow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(snow)


def make_mock_response(
    status: int = 200,
    json_data: Any = None,
    text: str = "",
    headers: Optional[dict] = None,
) -> MagicMock:
    """Helper to mock urllib.request.urlopen response."""
    resp = MagicMock()
    resp.status = status
    resp.code = status
    if json_data is not None:
        body = json.dumps(json_data).encode("utf-8")
    else:
        body = text.encode("utf-8")
    resp.read.return_value = body
    resp.headers = headers or {}
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = None
    return resp


# ---------------------------------------------------------------------------
# Query Construction Tests
# ---------------------------------------------------------------------------
class TestQueryConstruction:
    def test_default_older_than_when_no_filter_provided(self):
        """When no explicit filters are provided, args defaults to --older-than 7."""
        args = snow.parse_arguments(["--instance-url", "https://example.service-now.com", "--username", "admin"])
        assert args.older_than == 7
        query = snow.build_encoded_query(older_than=args.older_than)
        assert query == "sys_created_on<javascript:gs.daysAgoStart(7)"

    def test_explicit_older_than(self):
        """Explicit --older-than N constructs exact daysAgoStart clause."""
        args = snow.parse_arguments([
            "--older-than", "14",
            "--instance-url", "https://example.service-now.com",
            "--username", "admin",
        ])
        assert args.older_than == 14
        query = snow.build_encoded_query(older_than=args.older_than)
        assert query == "sys_created_on<javascript:gs.daysAgoStart(14)"

    def test_created_before_and_after_iso(self):
        """ISO timestamps for created-before and created-after are converted to UTC."""
        query = snow.build_encoded_query(
            created_before="2026-10-15T12:00:00Z",
            created_after="2026-10-01",
        )
        assert "sys_created_on<2026-10-15 12:00:00" in query
        assert "sys_created_on>2026-10-01 00:00:00" in query
        assert "^" in query

    def test_number_range(self):
        """Number range generates >= and <= clauses."""
        query = snow.build_encoded_query(number_from="INC0010000", number_to="INC0010050")
        assert query == "number>=INC0010000^number<=INC0010050"

    def test_number_from_only(self):
        query = snow.build_encoded_query(number_from="INC0010000")
        assert query == "number>=INC0010000"

    def test_number_to_only(self):
        query = snow.build_encoded_query(number_to="INC0010050")
        assert query == "number<=INC0010050"

    def test_numbers_list(self):
        """Explicit list of numbers creates numberIN clause."""
        query = snow.build_encoded_query(numbers=["INC0010222", "INC0010223"])
        assert query == "numberININC0010222,INC0010223"

    def test_caller_name_and_sys_id(self):
        """Caller username/name creates OR clause, while 32-hex creates exact sys_id match."""
        query_name = snow.build_encoded_query(caller="admin")
        assert query_name == "caller_id.user_name=admin^ORcaller_id.name=admin"

        hex_sys_id = "0123456789abcdef0123456789abcdef"
        query_sys_id = snow.build_encoded_query(caller=hex_sys_id)
        assert query_sys_id == f"caller_id={hex_sys_id}"

    def test_short_description_contains_multiple(self):
        """Multiple short description contains are combined with ^OR."""
        query = snow.build_encoded_query(short_description_contains=["database", "latency"])
        assert query == "short_descriptionLIKEdatabase^ORshort_descriptionLIKElatency"

    def test_state_mapping(self):
        """State names (new, closed, etc.) are converted to standard numeric codes."""
        query = snow.build_encoded_query(state=["new", "closed", "in_progress"])
        assert query == "stateIN1,7,2"

    def test_state_invalid_raises_error(self):
        with pytest.raises(ValueError, match="Invalid incident state 'invalid_state'"):
            snow.build_encoded_query(state=["invalid_state"])

    def test_priority_valid_and_invalid(self):
        query = snow.build_encoded_query(priority=["1", "2"])
        assert query == "priorityIN1,2"

        with pytest.raises(ValueError, match="Invalid incident priority '9'"):
            snow.build_encoded_query(priority=["9"])

    def test_assignment_group_category_and_created_by(self):
        query = snow.build_encoded_query(
            assignment_group="SRE Team",
            category="Network",
            created_by="integration_user",
        )
        assert "assignment_group.name=SRE Team" in query
        assert "category=Network" in query
        assert "sys_created_by=integration_user" in query

    def test_raw_query_and_combination(self):
        """Raw encoded query is appended with AND (^)."""
        query = snow.build_encoded_query(older_than=7, raw_query="active=true^priority=1")
        assert query == "sys_created_on<javascript:gs.daysAgoStart(7)^active=true^priority=1"

    def test_no_default_older_than_when_other_filter_specified(self):
        """When an explicit filter like --numbers is given, older-than is NOT applied by default."""
        args = snow.parse_arguments([
            "--numbers", "INC0010001,INC0010002",
            "--instance-url", "https://example.service-now.com",
            "--username", "admin",
        ])
        assert args.older_than is None
        assert args.numbers == ["INC0010001", "INC0010002"]

    @pytest.mark.parametrize(
        "kwargs,field_name",
        [
            ({"caller": "admin^ORactive=true"}, "caller"),
            ({"category": "Hardware\nactive=false"}, "category"),
            ({"assignment_group": "SRE\rTeam"}, "assignment-group"),
            ({"created_by": "user^injection"}, "created-by"),
            ({"number_from": "INC001^"}, "number-from"),
            ({"number_to": "INC002\n"}, "number-to"),
            ({"numbers": ["INC001", "INC002^extra"]}, "numbers"),
            ({"short_description_contains": ["database^ORpriority=1"]}, "short-description-contains"),
            ({"state": ["new^ORactive=true"]}, "state"),
            ({"priority": ["1\n"]}, "priority"),
            ({"created_before": "2026-10-01^"}, "created-before"),
            ({"created_after": "2026-10-01\r"}, "created-after"),
        ],
    )
    def test_injection_prevention_rejects_caret_and_newlines(self, kwargs, field_name):
        """Filters reject caret (^), newline, and carriage return to prevent clause injection."""
        with pytest.raises(ValueError, match=f"Invalid character in filter '{field_name}'"):
            snow.build_encoded_query(**kwargs)

    def test_raw_query_rejects_newlines_and_carriage_returns(self):
        """Raw query allows caret for syntax but strictly rejects newlines and carriage returns."""
        with pytest.raises(ValueError, match="Invalid character in filter 'query'"):
            snow.build_encoded_query(raw_query="active=true\npriority=1")

        with pytest.raises(ValueError, match="Invalid character in filter 'query'"):
            snow.build_encoded_query(raw_query="active=true\rpriority=1")


# ---------------------------------------------------------------------------
# Security & Credentials Tests
# ---------------------------------------------------------------------------
class TestSecurityAndCredentials:
    def test_password_flag_strictly_forbidden(self):
        """--password flag is rejected with explicit security error to guard shell history."""
        with pytest.raises(SystemExit) as exc_info:
            snow.parse_arguments(["--password", "supersecret"])
        assert "--password flag is strictly forbidden" in str(exc_info.value)

        with pytest.raises(SystemExit) as exc_info2:
            snow.parse_arguments(["--password=supersecret"])
        assert "--password flag is strictly forbidden" in str(exc_info2.value)

    def test_table_scope_locked_to_incident(self):
        """Script rejects any table other than 'incident'."""
        with pytest.raises(SystemExit) as exc_info:
            snow.parse_arguments(["--table", "problem"])
        assert "Table 'problem' is not supported" in str(exc_info.value)

    def test_credentials_resolved_from_env(self, monkeypatch):
        monkeypatch.setenv("SNOW_INSTANCE_URL", "https://env.service-now.com")
        monkeypatch.setenv("SNOW_USERNAME", "env_user")
        monkeypatch.setenv("SNOW_PASSWORD", "env_pass")

        url, user, password = snow.resolve_credentials(None, None, interactive=False)
        assert url == "https://env.service-now.com"
        assert user == "env_user"
        assert password == "env_pass"

    def test_credentials_password_interactive_prompt(self, monkeypatch):
        monkeypatch.delenv("SNOW_PASSWORD", raising=False)
        monkeypatch.delenv("SERVICENOW_PASSWORD", raising=False)

        with patch("sys.stdin.isatty", return_value=True), \
             patch("getpass.getpass", return_value="prompted_pass") as mock_getpass:
            url, user, password = snow.resolve_credentials("https://test.service-now.com", "admin", interactive=True)
            assert password == "prompted_pass"
            mock_getpass.assert_called_once()

    def test_credentials_missing_password_raises(self, monkeypatch):
        monkeypatch.delenv("SNOW_PASSWORD", raising=False)
        monkeypatch.delenv("SERVICENOW_PASSWORD", raising=False)

        with patch("sys.stdin.isatty", return_value=False):
            with pytest.raises(ValueError, match="ServiceNow Password is required"):
                snow.resolve_credentials("https://test.service-now.com", "admin", interactive=False)

    def test_logger_masks_passwords_and_tokens(self, caplog):
        """SensitiveDataFilter masks passwords and basic auth headers from log messages."""
        secret_password = "SuperSecretPassword123!"
        logger = snow.setup_logger(verbose=True, secrets_to_mask=[secret_password])

        with caplog.at_level(logging.DEBUG):
            logger.info("Connecting with password: %s", secret_password)
            logger.info("Auth header: Basic dXNlcjpwYXNzd29yZA==")
            logger.info("Payload: password=SuperSecretPassword123!&username=admin")

        log_text = caplog.text
        assert secret_password not in log_text
        assert "Connecting with password: ********" in log_text
        assert "Auth header: Basic ********" in log_text
        assert "password=********" in log_text


# ---------------------------------------------------------------------------
# Dry-Run Mode Tests
# ---------------------------------------------------------------------------
class TestDryRunMode:
    @patch("urllib.request.urlopen")
    def test_dry_run_makes_zero_delete_calls(self, mock_urlopen, capsys, monkeypatch):
        """Dry-run queries Table API, prints output, and issues zero DELETE calls."""
        monkeypatch.setenv("SNOW_INSTANCE_URL", "https://test.service-now.com")
        monkeypatch.setenv("SNOW_USERNAME", "admin")
        monkeypatch.setenv("SNOW_PASSWORD", "secret")

        sample_records = [
            {
                "sys_id": "sys001",
                "number": "INC0010001",
                "short_description": "Database lock timeout",
                "sys_created_on": "2026-10-10 10:00:00",
                "state": "7",
                "priority": "1",
                "sys_created_by": "integration",
            },
            {
                "sys_id": "sys002",
                "number": "INC0010002",
                "short_description": "Apigee gateway 504",
                "sys_created_on": "2026-10-11 11:00:00",
                "state": "6",
                "priority": "2",
                "sys_created_by": "integration",
            },
        ]

        mock_resp = make_mock_response(status=200, json_data={"result": sample_records})
        mock_urlopen.return_value = mock_resp

        exit_code = snow.main(["--older-than", "7"])
        assert exit_code == 0

        # Assert ZERO delete requests were executed
        assert not any(c[0][0].method == "DELETE" for c in mock_urlopen.call_args_list)

        captured = capsys.readouterr().out
        assert "[DRY-RUN MODE] Found 2 matching incident(s). Zero deletions executed." in captured
        assert "INC0010001" in captured
        assert "INC0010002" in captured
        assert "Database lock timeout" in captured

    @patch("urllib.request.urlopen")
    def test_dry_run_json_output(self, mock_urlopen, capsys, monkeypatch):
        monkeypatch.setenv("SNOW_INSTANCE_URL", "https://test.service-now.com")
        monkeypatch.setenv("SNOW_USERNAME", "admin")
        monkeypatch.setenv("SNOW_PASSWORD", "secret")

        sample_records = [{"sys_id": "sys001", "number": "INC0010001", "short_description": "Test"}]
        mock_resp = make_mock_response(status=200, json_data={"result": sample_records})
        mock_urlopen.return_value = mock_resp

        exit_code = snow.main(["--output", "json"])
        assert exit_code == 0
        assert not any(c[0][0].method == "DELETE" for c in mock_urlopen.call_args_list)

        out = capsys.readouterr().out
        assert '"INC0010001"' in out

    @patch("urllib.request.urlopen")
    def test_dry_run_csv_output(self, mock_urlopen, capsys, monkeypatch):
        monkeypatch.setenv("SNOW_INSTANCE_URL", "https://test.service-now.com")
        monkeypatch.setenv("SNOW_USERNAME", "admin")
        monkeypatch.setenv("SNOW_PASSWORD", "secret")

        sample_records = [{"sys_id": "sys001", "number": "INC0010001", "short_description": "Test"}]
        mock_resp = make_mock_response(status=200, json_data={"result": sample_records})
        mock_urlopen.return_value = mock_resp

        exit_code = snow.main(["--output", "csv"])
        assert exit_code == 0
        assert not any(c[0][0].method == "DELETE" for c in mock_urlopen.call_args_list)

        out = capsys.readouterr().out
        assert "sys_id,number,short_description" in out
        assert "sys001,INC0010001,Test" in out

    @patch("urllib.request.urlopen")
    def test_dry_run_zero_matches(self, mock_urlopen, capsys, monkeypatch):
        monkeypatch.setenv("SNOW_INSTANCE_URL", "https://test.service-now.com")
        monkeypatch.setenv("SNOW_USERNAME", "admin")
        monkeypatch.setenv("SNOW_PASSWORD", "secret")

        mock_resp = make_mock_response(status=200, json_data={"result": []})
        mock_urlopen.return_value = mock_resp

        exit_code = snow.main([])
        assert exit_code == 0
        assert not any(c[0][0].method == "DELETE" for c in mock_urlopen.call_args_list)

        out = capsys.readouterr().out
        assert "No matching incidents found." in out


# ---------------------------------------------------------------------------
# Apply Mode & Confirmation Tests
# ---------------------------------------------------------------------------
class TestApplyMode:
    @patch("urllib.request.urlopen")
    def test_apply_mismatch_confirmation_aborts_without_deleting(self, mock_urlopen, capsys, monkeypatch):
        """When confirmation does not match the exact count, abort without deleting."""
        monkeypatch.setenv("SNOW_INSTANCE_URL", "https://test.service-now.com")
        monkeypatch.setenv("SNOW_USERNAME", "admin")
        monkeypatch.setenv("SNOW_PASSWORD", "secret")

        sample_records = [
            {"sys_id": "sys001", "number": "INC0010001", "short_description": "Alert 1"},
            {"sys_id": "sys002", "number": "INC0010002", "short_description": "Alert 2"},
        ]
        mock_resp = make_mock_response(status=200, json_data={"result": sample_records})
        mock_urlopen.return_value = mock_resp

        # Operator inputs '1' instead of exact count '2'
        with patch("builtins.input", return_value="1"):
            exit_code = snow.main(["--apply"])

        assert exit_code == 1
        assert not any(c[0][0].method == "DELETE" for c in mock_urlopen.call_args_list)

        out = capsys.readouterr().out
        assert "Aborted: Confirmation '1' did not match incident count '2'." in out
        assert "Zero incidents were deleted." in out

    @patch("urllib.request.urlopen")
    def test_apply_exact_confirmation_deletes_records(self, mock_urlopen, capsys, monkeypatch):
        """Matching confirmation executes DELETE for each sys_id."""
        monkeypatch.setenv("SNOW_INSTANCE_URL", "https://test.service-now.com")
        monkeypatch.setenv("SNOW_USERNAME", "admin")
        monkeypatch.setenv("SNOW_PASSWORD", "secret")

        sample_records = [
            {"sys_id": "sys001", "number": "INC0010001", "short_description": "Alert 1"},
            {"sys_id": "sys002", "number": "INC0010002", "short_description": "Alert 2"},
        ]
        mock_get_resp = make_mock_response(status=200, json_data={"result": sample_records})
        mock_del_resp = make_mock_response(status=204)
        mock_urlopen.side_effect = [mock_get_resp, mock_del_resp, mock_del_resp]

        with patch("builtins.input", return_value="2"):
            exit_code = snow.main(["--apply"])

        assert exit_code == 0
        del_calls = [c for c in mock_urlopen.call_args_list if c[0][0].method == "DELETE"]
        assert len(del_calls) == 2
        assert del_calls[0][0][0].full_url == "https://test.service-now.com/api/now/table/incident/sys001"
        assert del_calls[1][0][0].full_url == "https://test.service-now.com/api/now/table/incident/sys002"

        out = capsys.readouterr().out
        assert "Total targeted:       2" in out
        assert "Successfully deleted: 2" in out
        assert "Failed deletions:     0" in out
        assert "Child attachments were removed by ServiceNow database cascade." in out

    @patch("urllib.request.urlopen")
    def test_apply_with_yes_flag_non_interactive(self, mock_urlopen, capsys, monkeypatch):
        """--yes skips input prompt and executes deletions with count echoed."""
        monkeypatch.setenv("SNOW_INSTANCE_URL", "https://test.service-now.com")
        monkeypatch.setenv("SNOW_USERNAME", "admin")
        monkeypatch.setenv("SNOW_PASSWORD", "secret")

        sample_records = [{"sys_id": "sys001", "number": "INC0010001", "short_description": "Alert"}]
        mock_get_resp = make_mock_response(status=200, json_data={"result": sample_records})
        mock_del_resp = make_mock_response(status=204)
        mock_urlopen.side_effect = [mock_get_resp, mock_del_resp]

        with patch("builtins.input") as mock_input:
            exit_code = snow.main(["--apply", "--yes"])

        mock_input.assert_not_called()
        assert exit_code == 0
        del_calls = [c for c in mock_urlopen.call_args_list if c[0][0].method == "DELETE"]
        assert len(del_calls) == 1
        assert del_calls[0][0][0].full_url == "https://test.service-now.com/api/now/table/incident/sys001"

        out = capsys.readouterr().out
        assert "Confirming deletion of 1 incidents (--yes specified)." in out
        assert "Successfully deleted: 1" in out

    @patch("urllib.request.urlopen")
    def test_apply_zero_records_found(self, mock_urlopen, capsys, monkeypatch):
        monkeypatch.setenv("SNOW_INSTANCE_URL", "https://test.service-now.com")
        monkeypatch.setenv("SNOW_USERNAME", "admin")
        monkeypatch.setenv("SNOW_PASSWORD", "secret")

        mock_resp = make_mock_response(status=200, json_data={"result": []})
        mock_urlopen.return_value = mock_resp

        exit_code = snow.main(["--apply"])
        assert exit_code == 0
        assert not any(c[0][0].method == "DELETE" for c in mock_urlopen.call_args_list)
        assert "Nothing to delete." in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Paging, Safety Limits, and Regex Filtering
# ---------------------------------------------------------------------------
class TestPagingAndLimits:
    def test_multi_page_pagination(self):
        """Paging increments sysparm_offset and retrieves records across pages."""
        session = MagicMock()

        # Page 1: 100 records; Page 2: 30 records
        page1 = [{"sys_id": f"s_{i}", "number": f"INC_{i}"} for i in range(100)]
        page2 = [{"sys_id": f"s_{i}", "number": f"INC_{i}"} for i in range(100, 130)]

        resp1 = MagicMock()
        resp1.status_code = 200
        resp1.json.return_value = {"result": page1}

        resp2 = MagicMock()
        resp2.status_code = 200
        resp2.json.return_value = {"result": page2}

        session.get.side_effect = [resp1, resp2]

        results = snow.fetch_incidents(
            session=session,
            instance_url="https://test.service-now.com",
            query="active=true",
            limit=500,
            page_size=100,
        )

        assert len(results) == 130
        assert session.get.call_count == 2
        # Verify offsets
        call1_params = session.get.call_args_list[0][1]["params"]
        call2_params = session.get.call_args_list[1][1]["params"]
        assert call1_params["sysparm_offset"] == "0"
        assert call2_params["sysparm_offset"] == "100"

    def test_limit_capping(self):
        """Fetch stops once limit is reached."""
        session = MagicMock()
        records = [{"sys_id": f"s_{i}", "number": f"INC_{i}"} for i in range(100)]
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"result": records}
        session.get.return_value = resp

        results = snow.fetch_incidents(
            session=session,
            instance_url="https://test.service-now.com",
            query="active=true",
            limit=25,
            page_size=100,
        )
        assert len(results) == 25

    def test_client_side_short_description_regex(self):
        """Client-side regex pattern filters fetched records."""
        session = MagicMock()
        raw_records = [
            {"sys_id": "1", "number": "INC1", "short_description": "CloudSQL connection pool exhausted"},
            {"sys_id": "2", "number": "INC2", "short_description": "Apigee 502 Bad Gateway"},
            {"sys_id": "3", "number": "INC3", "short_description": "CloudSQL lock contention detected"},
        ]
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"result": raw_records}
        session.get.return_value = resp

        results = snow.fetch_incidents(
            session=session,
            instance_url="https://test.service-now.com",
            query="active=true",
            short_description_regex=r"CloudSQL.*",
        )
        assert len(results) == 2
        assert {r["number"] for r in results} == {"INC1", "INC3"}

    def test_exceeding_max_limit_aborts(self):
        with pytest.raises(SystemExit) as exc_info:
            snow.parse_arguments(["--limit", "20000", "--max-limit", "10000"])
        assert "exceeds safety maximum ceiling of 10000" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Retry and Backoff Tests
# ---------------------------------------------------------------------------
class TestDeleteRetriesAndBackoff:
    def test_delete_retries_on_transient_error_and_succeeds(self):
        """DELETE retries on HTTP 429 and succeeds on subsequent 204."""
        session = MagicMock()

        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.text = "Too Many Requests"

        resp_204 = MagicMock()
        resp_204.status_code = 204

        session.delete.side_effect = [resp_429, resp_204]

        with patch("time.sleep") as mock_sleep:
            successful, failures = snow.delete_incidents(
                session=session,
                instance_url="https://test.service-now.com",
                incidents=[{"sys_id": "sys01", "number": "INC01"}],
                max_retries=2,
                initial_backoff=0.01,
            )

        assert successful == ["INC01"]
        assert len(failures) == 0
        assert session.delete.call_count == 2
        mock_sleep.assert_called_once()

    def test_delete_retries_exhausted_records_failure(self):
        """DELETE retries up to max_retries on 500 and records failure."""
        session = MagicMock()

        resp_500 = MagicMock()
        resp_500.status_code = 500
        resp_500.text = "Internal Server Error"

        session.delete.return_value = resp_500

        with patch("time.sleep"):
            successful, failures = snow.delete_incidents(
                session=session,
                instance_url="https://test.service-now.com",
                incidents=[{"sys_id": "sys01", "number": "INC01"}],
                max_retries=2,
                initial_backoff=0.01,
            )

        assert len(successful) == 0
        assert len(failures) == 1
        assert failures[0]["number"] == "INC01"
        assert "HTTP 500" in failures[0]["error"]
        assert session.delete.call_count == 3  # Initial + 2 retries

    def test_delete_404_treated_as_already_removed(self):
        """HTTP 404 is treated as successfully removed."""
        session = MagicMock()
        resp_404 = MagicMock()
        resp_404.status_code = 404
        session.delete.return_value = resp_404

        successful, failures = snow.delete_incidents(
            session=session,
            instance_url="https://test.service-now.com",
            incidents=[{"sys_id": "sys01", "number": "INC01"}],
        )
        assert successful == ["INC01"]
        assert len(failures) == 0

    def test_delete_empty_or_whitespace_sys_id_skipped_and_recorded(self):
        """Records with empty or whitespace sys_id are skipped without DELETE calls and recorded as failures."""
        session = MagicMock()
        resp_204 = MagicMock()
        resp_204.status_code = 204
        session.delete.return_value = resp_204

        incidents_payload = [
            {"sys_id": "", "number": "INC_NO_SYSID"},
            {"sys_id": "   ", "number": "INC_BLANK_SYSID"},
            {"sys_id": None, "number": "INC_NONE_SYSID"},
            {"sys_id": "valid_sys_01", "number": "INC_VALID_01"},
        ]

        successful, failures = snow.delete_incidents(
            session=session,
            instance_url="https://test.service-now.com",
            incidents=incidents_payload,
        )

        # Only the valid sys_id had DELETE issued
        assert session.delete.call_count == 1
        session.delete.assert_called_once_with(
            "https://test.service-now.com/api/now/table/incident/valid_sys_01",
            timeout=30,
        )

        assert successful == ["INC_VALID_01"]
        assert len(failures) == 3
        for fail in failures:
            assert "missing or empty sys_id" in fail["error"]
        assert [f["number"] for f in failures] == ["INC_NO_SYSID", "INC_BLANK_SYSID", "INC_NONE_SYSID"]


# ---------------------------------------------------------------------------
# HTTP Client Helper Tests
# ---------------------------------------------------------------------------
class TestHttpSession:
    def test_basic_auth_header_encoding(self):
        """Basic auth header is correctly base64-encoded."""
        session = snow.HttpSession()
        session.set_basic_auth("myuser", "mypassword")
        assert session.headers["Authorization"] == "Basic bXl1c2VyOm15cGFzc3dvcmQ="

    @patch("urllib.request.urlopen")
    def test_get_with_params_and_tls_context(self, mock_urlopen):
        """GET request formats query params and enforces TLS default context for https."""
        mock_urlopen.return_value = make_mock_response(status=200, json_data={"result": []})
        session = snow.HttpSession()
        session.set_basic_auth("admin", "secret")
        resp = session.get("https://test.service-now.com/api/test", params={"limit": "10", "offset": "0"})

        assert resp.status_code == 200
        assert resp.json() == {"result": []}
        assert mock_urlopen.call_count == 1
        req = mock_urlopen.call_args[0][0]
        context = mock_urlopen.call_args[1].get("context")
        assert context is not None
        assert "limit=10" in req.full_url
        assert "offset=0" in req.full_url
        assert req.headers["Authorization"] == "Basic YWRtaW46c2VjcmV0"

    @patch("urllib.request.urlopen")
    def test_http_error_preserves_status_code_and_headers(self, mock_urlopen):
        """urllib.error.HTTPError preserves HTTP status code, headers, and body."""
        http_err = urllib.error.HTTPError(
            "https://test.service-now.com/api/test",
            429,
            "Too Many Requests",
            {"Retry-After": "5", "Content-Type": "application/json"},
            io.BytesIO(b'{"error": "rate limited"}'),
        )
        mock_urlopen.side_effect = http_err

        session = snow.HttpSession()
        resp = session.delete("https://test.service-now.com/api/test")
        assert resp.status_code == 429
        assert resp.headers.get("Retry-After") == "5"
        assert resp.text == '{"error": "rate limited"}'

        with pytest.raises(snow.HTTPStatusError, match="HTTP 429"):
            resp.raise_for_status()

    @patch("urllib.request.urlopen")
    def test_network_error_raises_request_error(self, mock_urlopen):
        """urllib.error.URLError raises RequestError."""
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
        session = snow.HttpSession()
        with pytest.raises(snow.RequestError, match="Network error: Connection refused"):
            session.get("https://test.service-now.com/api/test")

    @patch("urllib.request.urlopen")
    def test_https_scheme_case_insensitive(self, mock_urlopen):
        """TLS verification context is created for HTTPS scheme regardless of casing."""
        mock_urlopen.return_value = make_mock_response(status=200, json_data={"result": []})
        session = snow.HttpSession()

        session.get("HTTPS://test.service-now.com/api/test")
        context_upper = mock_urlopen.call_args[1].get("context")
        assert context_upper is not None

        session.get("Https://test.service-now.com/api/test")
        context_mixed = mock_urlopen.call_args[1].get("context")
        assert context_mixed is not None

        session.get("http://test.service-now.com/api/test")
        context_http = mock_urlopen.call_args[1].get("context")
        assert context_http is None


class TestTLSCertificateHandling:
    """Tests for CA bundle resolution, TLS verification enforcement, and cert verification error hints."""

    def test_cafile_precedence_flag_over_env_and_certifi(self, monkeypatch):
        """CLI flag takes precedence over environment variables and certifi."""
        monkeypatch.setenv("SSL_CERT_FILE", "/env/ssl_cert.pem")
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/env/requests_ca.pem")
        mock_certifi = types.ModuleType("certifi")
        mock_certifi.where = lambda: "/certifi/where.pem"
        monkeypatch.setitem(sys.modules, "certifi", mock_certifi)

        resolved = snow.resolve_ca_bundle(ca_bundle="/flag/custom_ca.pem")
        assert resolved == "/flag/custom_ca.pem"

    def test_cafile_precedence_env_ssl_cert_file_over_requests_ca_bundle_and_certifi(self, monkeypatch):
        """SSL_CERT_FILE takes precedence over REQUESTS_CA_BUNDLE and certifi when no flag is given."""
        monkeypatch.setenv("SSL_CERT_FILE", "/env/ssl_cert.pem")
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/env/requests_ca.pem")
        mock_certifi = types.ModuleType("certifi")
        mock_certifi.where = lambda: "/certifi/where.pem"
        monkeypatch.setitem(sys.modules, "certifi", mock_certifi)

        resolved = snow.resolve_ca_bundle(ca_bundle=None)
        assert resolved == "/env/ssl_cert.pem"

    def test_cafile_precedence_env_requests_ca_bundle_over_certifi(self, monkeypatch):
        """REQUESTS_CA_BUNDLE takes precedence over certifi when SSL_CERT_FILE is unset."""
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/env/requests_ca.pem")
        mock_certifi = types.ModuleType("certifi")
        mock_certifi.where = lambda: "/certifi/where.pem"
        monkeypatch.setitem(sys.modules, "certifi", mock_certifi)

        resolved = snow.resolve_ca_bundle(ca_bundle=None)
        assert resolved == "/env/requests_ca.pem"

    def test_cafile_precedence_env_empty_or_whitespace_falls_through(self, monkeypatch):
        """Empty or whitespace-only environment variables are ignored and stripped, falling through."""
        # 1. Whitespace in SSL_CERT_FILE falls through to valid REQUESTS_CA_BUNDLE (and strips whitespace)
        monkeypatch.setenv("SSL_CERT_FILE", "   ")
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", "  /env/requests_ca.pem  ")
        assert snow.resolve_ca_bundle(ca_bundle=None) == "/env/requests_ca.pem"

        # 2. Both empty/whitespace falls through to certifi
        monkeypatch.setenv("SSL_CERT_FILE", "")
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", "   \t\n  ")
        mock_certifi = types.ModuleType("certifi")
        mock_certifi.where = lambda: "/certifi/where.pem"
        monkeypatch.setitem(sys.modules, "certifi", mock_certifi)
        assert snow.resolve_ca_bundle(ca_bundle=None) == "/certifi/where.pem"

        # 3. SSL_CERT_FILE with padding whitespace is stripped
        monkeypatch.setenv("SSL_CERT_FILE", "  /env/ssl_cert.pem  ")
        assert snow.resolve_ca_bundle(ca_bundle=None) == "/env/ssl_cert.pem"

    def test_cafile_precedence_certifi_over_default(self, monkeypatch):
        """certifi.where() is used when no flag or environment variables are provided."""
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
        mock_certifi = types.ModuleType("certifi")
        mock_certifi.where = lambda: "/certifi/where.pem"
        monkeypatch.setitem(sys.modules, "certifi", mock_certifi)

        resolved = snow.resolve_ca_bundle(ca_bundle=None)
        assert resolved == "/certifi/where.pem"

    def test_cafile_precedence_default_when_no_certifi(self, monkeypatch):
        """Returns None (system default) when no flag, no env, certifi is missing, and not macOS with cert.pem."""
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
        monkeypatch.setitem(sys.modules, "certifi", None)
        monkeypatch.setattr(sys, "platform", "linux")

        resolved = snow.resolve_ca_bundle(ca_bundle=None)
        assert resolved is None

    def test_cafile_precedence_macos_etc_ssl_cert_pem_over_default(self, monkeypatch):
        """On macOS, falls back to /etc/ssl/cert.pem if certifi is not importable."""
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
        monkeypatch.setitem(sys.modules, "certifi", None)
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(os.path, "exists", lambda p: p == "/etc/ssl/cert.pem")

        resolved = snow.resolve_ca_bundle(ca_bundle=None)
        assert resolved == "/etc/ssl/cert.pem"

    def test_cafile_precedence_macos_etc_ssl_cert_pem_ignored_on_non_darwin(self, monkeypatch):
        """On non-Darwin platforms, /etc/ssl/cert.pem is not used as an automatic fallback."""
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
        monkeypatch.setitem(sys.modules, "certifi", None)
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(os.path, "exists", lambda p: p == "/etc/ssl/cert.pem")

        resolved = snow.resolve_ca_bundle(ca_bundle=None)
        assert resolved is None

    def test_cafile_precedence_certifi_over_macos_etc_ssl_cert_pem(self, monkeypatch):
        """certifi takes precedence over macOS /etc/ssl/cert.pem fallback."""
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
        mock_certifi = types.ModuleType("certifi")
        mock_certifi.where = lambda: "/certifi/where.pem"
        monkeypatch.setitem(sys.modules, "certifi", mock_certifi)
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(os.path, "exists", lambda p: p == "/etc/ssl/cert.pem")

        resolved = snow.resolve_ca_bundle(ca_bundle=None)
        assert resolved == "/certifi/where.pem"

    @patch("ssl.create_default_context")
    def test_create_ssl_context_calls_create_default_context_with_cafile(self, mock_create_ctx):
        """create_ssl_context resolves bundle and passes cafile to ssl.create_default_context."""
        mock_ctx = MagicMock()
        mock_create_ctx.return_value = mock_ctx

        ctx = snow.create_ssl_context(ca_bundle="/path/to/my_bundle.pem")
        mock_create_ctx.assert_called_once_with(cafile="/path/to/my_bundle.pem")
        assert ctx == mock_ctx

    def test_tls_verification_never_disabled(self):
        """TLS certificate verification and hostname checking are never disabled."""
        ctx_default = snow.create_ssl_context(ca_bundle=None)
        assert ctx_default.verify_mode == ssl.CERT_REQUIRED
        assert ctx_default.check_hostname is True

        # Insecure bypass flags must NOT exist
        parser = snow.build_arg_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--insecure"])
        with pytest.raises(SystemExit):
            parser.parse_args(["--no-verify"])
        with pytest.raises(SystemExit):
            parser.parse_args(["-k"])

    def test_is_cert_verification_error_detection(self):
        """is_cert_verification_error correctly identifies SSL verification errors across structures."""
        cert_err = ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
        url_err = urllib.error.URLError(cert_err)
        req_err = snow.RequestError(f"Network error: {url_err.reason}")
        req_err.__cause__ = url_err

        assert snow.is_cert_verification_error(cert_err) is True
        assert snow.is_cert_verification_error(url_err) is True
        assert snow.is_cert_verification_error(req_err) is True
        assert snow.is_cert_verification_error(snow.RequestError("Network error: [SSL: CERTIFICATE_VERIFY_FAILED] ...")) is True

        # Non-verification errors should return False
        assert snow.is_cert_verification_error(snow.RequestError("Network error: Connection refused")) is False
        assert snow.is_cert_verification_error(urllib.error.URLError("Connection reset")) is False
        assert snow.is_cert_verification_error(ValueError("Invalid argument")) is False

    @patch("urllib.request.urlopen")
    def test_cert_verification_failure_in_main_prints_hint_and_no_traceback(self, mock_urlopen, capsys, monkeypatch):
        """main exits non-zero and prints macOS/certifi/ca-bundle hint with no traceback on CERTIFICATE_VERIFY_FAILED."""
        monkeypatch.setenv("SNOW_PASSWORD", "secret")
        cert_err = ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed (_ssl.c:1029)")
        mock_urlopen.side_effect = urllib.error.URLError(cert_err)

        exit_code = snow.main([
            "--instance-url", "https://test.service-now.com",
            "--username", "admin",
            "--older-than", "10",
        ])

        assert exit_code == 1
        captured = capsys.readouterr()
        err_out = captured.err

        # Check required hint contents
        assert "Install Certificates.command" in err_out
        assert "/etc/ssl/cert.pem" in err_out
        assert "pip install certifi" in err_out
        assert "--ca-bundle" in err_out
        assert "SSL_CERT_FILE" in err_out

        # Verify NO traceback is output
        assert "Traceback (most recent call last)" not in err_out
        assert "Traceback (most recent call last)" not in captured.out

    def test_missing_ca_bundle_file_in_main_exits_nonzero(self, capsys):
        """Specifying a nonexistent CA bundle file exits non-zero with a clear error."""
        exit_code = snow.main([
            "--instance-url", "https://test.service-now.com",
            "--username", "admin",
            "--older-than", "10",
            "--ca-bundle", "/path/to/definitely_nonexistent_bundle.pem",
        ])
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "Configuration Error: CA bundle file not found" in captured.err

    @patch("urllib.request.urlopen")
    def test_cert_verification_failure_in_delete_prints_hint(self, mock_urlopen, capsys):
        """delete_incidents detects cert failure, prints hint, and aborts immediately without retrying."""
        cert_err = ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
        mock_urlopen.side_effect = urllib.error.URLError(cert_err)

        session = snow.HttpSession()
        successful, failures = snow.delete_incidents(
            session=session,
            instance_url="https://test.service-now.com",
            incidents=[{"sys_id": "sys123", "number": "INC001"}],
        )

        assert len(successful) == 0
        assert len(failures) == 1
        assert "CERTIFICATE_VERIFY_FAILED" in failures[0]["error"]
        assert mock_urlopen.call_count == 1  # No wasteful retries on cert failure

        captured = capsys.readouterr()
        assert "Install Certificates.command" in captured.err
        assert "pip install certifi" in captured.err

    def test_ca_bundle_arg_passed_to_session(self, tmp_path, monkeypatch):
        """Valid --ca-bundle file is configured on HttpSession in main."""
        monkeypatch.setenv("SNOW_PASSWORD", "secret")
        fake_ca = tmp_path / "custom_ca.pem"
        fake_ca.write_text("-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----\n")

        session_instances = []
        original_init = snow.HttpSession.__init__

        def mock_session_init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            session_instances.append(self)

        monkeypatch.setattr(snow.HttpSession, "__init__", mock_session_init)
        monkeypatch.setattr(snow, "fetch_incidents", lambda *args, **kwargs: [])

        exit_code = snow.main([
            "--instance-url", "https://test.service-now.com",
            "--username", "admin",
            "--older-than", "10",
            "--ca-bundle", str(fake_ca),
        ])

        assert exit_code == 0
        assert len(session_instances) == 1
        assert session_instances[0].ca_bundle == str(fake_ca)
