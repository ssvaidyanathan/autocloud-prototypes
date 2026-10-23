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
import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
import requests

# Dynamically import servicenow-cleanup.py (hyphenated script name)
SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "servicenow-cleanup.py"
spec = importlib.util.spec_from_file_location("servicenow_cleanup", SCRIPT_PATH)
if spec is None or spec.loader is None:
    raise ImportError(f"Cannot load spec from {SCRIPT_PATH}")
snow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(snow)


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
    @patch("requests.Session")
    def test_dry_run_makes_zero_delete_calls(self, mock_session_cls, capsys, monkeypatch):
        """Dry-run queries Table API, prints output, and issues zero DELETE calls."""
        monkeypatch.setenv("SNOW_INSTANCE_URL", "https://test.service-now.com")
        monkeypatch.setenv("SNOW_USERNAME", "admin")
        monkeypatch.setenv("SNOW_PASSWORD", "secret")

        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session

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

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"result": sample_records}
        mock_session.get.return_value = mock_resp

        exit_code = snow.main(["--older-than", "7"])
        assert exit_code == 0

        # Assert ZERO delete requests were executed
        assert mock_session.delete.call_count == 0

        captured = capsys.readouterr().out
        assert "[DRY-RUN MODE] Found 2 matching incident(s). Zero deletions executed." in captured
        assert "INC0010001" in captured
        assert "INC0010002" in captured
        assert "Database lock timeout" in captured

    @patch("requests.Session")
    def test_dry_run_json_output(self, mock_session_cls, capsys, monkeypatch):
        monkeypatch.setenv("SNOW_INSTANCE_URL", "https://test.service-now.com")
        monkeypatch.setenv("SNOW_USERNAME", "admin")
        monkeypatch.setenv("SNOW_PASSWORD", "secret")

        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        sample_records = [{"sys_id": "sys001", "number": "INC0010001", "short_description": "Test"}]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"result": sample_records}
        mock_session.get.return_value = mock_resp

        exit_code = snow.main(["--output", "json"])
        assert exit_code == 0
        assert mock_session.delete.call_count == 0

        out = capsys.readouterr().out
        assert '"INC0010001"' in out

    @patch("requests.Session")
    def test_dry_run_csv_output(self, mock_session_cls, capsys, monkeypatch):
        monkeypatch.setenv("SNOW_INSTANCE_URL", "https://test.service-now.com")
        monkeypatch.setenv("SNOW_USERNAME", "admin")
        monkeypatch.setenv("SNOW_PASSWORD", "secret")

        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        sample_records = [{"sys_id": "sys001", "number": "INC0010001", "short_description": "Test"}]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"result": sample_records}
        mock_session.get.return_value = mock_resp

        exit_code = snow.main(["--output", "csv"])
        assert exit_code == 0
        assert mock_session.delete.call_count == 0

        out = capsys.readouterr().out
        assert "sys_id,number,short_description" in out
        assert "sys001,INC0010001,Test" in out

    @patch("requests.Session")
    def test_dry_run_zero_matches(self, mock_session_cls, capsys, monkeypatch):
        monkeypatch.setenv("SNOW_INSTANCE_URL", "https://test.service-now.com")
        monkeypatch.setenv("SNOW_USERNAME", "admin")
        monkeypatch.setenv("SNOW_PASSWORD", "secret")

        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"result": []}
        mock_session.get.return_value = mock_resp

        exit_code = snow.main([])
        assert exit_code == 0
        assert mock_session.delete.call_count == 0

        out = capsys.readouterr().out
        assert "No matching incidents found." in out


# ---------------------------------------------------------------------------
# Apply Mode & Confirmation Tests
# ---------------------------------------------------------------------------
class TestApplyMode:
    @patch("requests.Session")
    def test_apply_mismatch_confirmation_aborts_without_deleting(self, mock_session_cls, capsys, monkeypatch):
        """When confirmation does not match the exact count, abort without deleting."""
        monkeypatch.setenv("SNOW_INSTANCE_URL", "https://test.service-now.com")
        monkeypatch.setenv("SNOW_USERNAME", "admin")
        monkeypatch.setenv("SNOW_PASSWORD", "secret")

        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        sample_records = [
            {"sys_id": "sys001", "number": "INC0010001", "short_description": "Alert 1"},
            {"sys_id": "sys002", "number": "INC0010002", "short_description": "Alert 2"},
        ]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"result": sample_records}
        mock_session.get.return_value = mock_resp

        # Operator inputs '1' instead of exact count '2'
        with patch("builtins.input", return_value="1"):
            exit_code = snow.main(["--apply"])

        assert exit_code == 1
        assert mock_session.delete.call_count == 0

        out = capsys.readouterr().out
        assert "Aborted: Confirmation '1' did not match incident count '2'." in out
        assert "Zero incidents were deleted." in out

    @patch("requests.Session")
    def test_apply_exact_confirmation_deletes_records(self, mock_session_cls, capsys, monkeypatch):
        """Matching confirmation executes DELETE for each sys_id."""
        monkeypatch.setenv("SNOW_INSTANCE_URL", "https://test.service-now.com")
        monkeypatch.setenv("SNOW_USERNAME", "admin")
        monkeypatch.setenv("SNOW_PASSWORD", "secret")

        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        sample_records = [
            {"sys_id": "sys001", "number": "INC0010001", "short_description": "Alert 1"},
            {"sys_id": "sys002", "number": "INC0010002", "short_description": "Alert 2"},
        ]
        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 200
        mock_get_resp.json.return_value = {"result": sample_records}
        mock_session.get.return_value = mock_get_resp

        # Mock successful DELETE calls (204 No Content)
        mock_del_resp = MagicMock()
        mock_del_resp.status_code = 204
        mock_session.delete.return_value = mock_del_resp

        with patch("builtins.input", return_value="2"):
            exit_code = snow.main(["--apply"])

        assert exit_code == 0
        assert mock_session.delete.call_count == 2
        mock_session.delete.assert_has_calls([
            call("https://test.service-now.com/api/now/table/incident/sys001", timeout=30),
            call("https://test.service-now.com/api/now/table/incident/sys002", timeout=30),
        ])

        out = capsys.readouterr().out
        assert "Total targeted:       2" in out
        assert "Successfully deleted: 2" in out
        assert "Failed deletions:     0" in out
        assert "Child attachments were removed by ServiceNow database cascade." in out

    @patch("requests.Session")
    def test_apply_with_yes_flag_non_interactive(self, mock_session_cls, capsys, monkeypatch):
        """--yes skips input prompt and executes deletions with count echoed."""
        monkeypatch.setenv("SNOW_INSTANCE_URL", "https://test.service-now.com")
        monkeypatch.setenv("SNOW_USERNAME", "admin")
        monkeypatch.setenv("SNOW_PASSWORD", "secret")

        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        sample_records = [{"sys_id": "sys001", "number": "INC0010001", "short_description": "Alert"}]
        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 200
        mock_get_resp.json.return_value = {"result": sample_records}
        mock_session.get.return_value = mock_get_resp

        mock_del_resp = MagicMock()
        mock_del_resp.status_code = 204
        mock_session.delete.return_value = mock_del_resp

        with patch("builtins.input") as mock_input:
            exit_code = snow.main(["--apply", "--yes"])

        mock_input.assert_not_called()
        assert exit_code == 0
        assert mock_session.delete.call_count == 1

        out = capsys.readouterr().out
        assert "Confirming deletion of 1 incidents (--yes specified)." in out
        assert "Successfully deleted: 1" in out

    @patch("requests.Session")
    def test_apply_zero_records_found(self, mock_session_cls, capsys, monkeypatch):
        monkeypatch.setenv("SNOW_INSTANCE_URL", "https://test.service-now.com")
        monkeypatch.setenv("SNOW_USERNAME", "admin")
        monkeypatch.setenv("SNOW_PASSWORD", "secret")

        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"result": []}
        mock_session.get.return_value = mock_resp

        exit_code = snow.main(["--apply"])
        assert exit_code == 0
        assert mock_session.delete.call_count == 0
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
