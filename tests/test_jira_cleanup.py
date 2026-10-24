"""
Unit tests for the Jira Cloud Incident and Issue Cleanup Utility (repo-root/scripts/jira-cleanup.py).

Verifies:
- JQL query construction for all filters (project, age, dates, keys, key range, type, status, labels, reporter, summary, raw JQL)
- Safe JQL quoting, escaping, and strict injection prevention
- Rejection of newline and carriage return characters across filters
- Strict API token and credential security (rejection of --token, --api-token, --password; getpass prompt; masked logs)
- Search pagination with nextPageToken and limit capping
- Client-side regular expression filtering on summary
- Delete retry and backoff honoring HTTP 429 Retry-After headers and handling transient 5xx errors
- Skip and record failures for records missing an issue key without calling DELETE
- Dry-run mode executes zero DELETE requests and supports table, CSV, and JSON output
- Apply mode confirmation count verification and non-interactive --yes execution
"""

from __future__ import annotations

import importlib.util
import io
import json
import logging
import ssl
import sys
import types
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

# Dynamically import jira-cleanup.py (hyphenated script name)
SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "jira-cleanup.py"
spec = importlib.util.spec_from_file_location("jira_cleanup", SCRIPT_PATH)
if spec is None or spec.loader is None:
    raise ImportError(f"Cannot load spec from {SCRIPT_PATH}")
jira = importlib.util.module_from_spec(spec)
spec.loader.exec_module(jira)


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
# JQL Query Construction Tests
# ---------------------------------------------------------------------------
class TestJQLQueryConstruction:
    def test_single_project(self):
        """Single project creates project = 'KEY' clause."""
        query = jira.build_jql_query(projects=["PROJ"])
        assert query == 'project = "PROJ"'

    def test_multiple_projects(self):
        """Multiple projects create project IN ('KEY1', 'KEY2') clause."""
        query = jira.build_jql_query(projects=["PROJ1", "PROJ2"])
        assert query == 'project IN ("PROJ1", "PROJ2")'

    def test_older_than(self):
        """Older-than creates created <= '-Nd' relative duration clause."""
        query = jira.build_jql_query(older_than=14)
        assert query == 'created <= "-14d"'

    def test_older_than_negative_raises_error(self):
        """Negative older-than values raise ValueError."""
        with pytest.raises(ValueError, match="must be a non-negative integer"):
            jira.build_jql_query(older_than=-1)

    def test_created_before_and_after_iso_date(self):
        """ISO date strings are parsed and formatted into JQL date bounds."""
        query = jira.build_jql_query(
            created_before="2026-10-15",
            created_after="2026-10-01",
        )
        assert 'created < "2026-10-15"' in query
        assert 'created > "2026-10-01"' in query
        assert " AND " in query

    def test_created_before_and_after_iso_datetime_utc(self):
        """ISO datetime with UTC timezone is parsed and formatted as YYYY-MM-DD HH:mm."""
        query = jira.build_jql_query(
            created_before="2026-10-15T12:00:00Z",
            created_after="2026-10-01T08:30:00+00:00",
        )
        assert 'created < "2026-10-15 12:00"' in query
        assert 'created > "2026-10-01 08:30"' in query

    def test_single_key(self):
        """Single explicit key creates key = 'KEY' clause."""
        query = jira.build_jql_query(keys=["PROJ-101"])
        assert query == 'key = "PROJ-101"'

    def test_multiple_keys(self):
        """Multiple explicit keys create key IN ('KEY1', 'KEY2') clause."""
        query = jira.build_jql_query(keys=["PROJ-101", "PROJ-102"])
        assert query == 'key IN ("PROJ-101", "PROJ-102")'

    def test_key_range_same_project(self):
        """Key range within same project creates key >= FROM AND key <= TO."""
        query = jira.build_jql_query(key_from="PROJ-100", key_to="PROJ-150")
        assert query == 'key >= "PROJ-100" AND key <= "PROJ-150"'

    def test_key_range_equal_bounds(self):
        """Key range with equal from and to bounds is permitted."""
        query = jira.build_jql_query(key_from="PROJ-100", key_to="PROJ-100")
        assert query == 'key >= "PROJ-100" AND key <= "PROJ-100"'

    def test_key_range_numeric_order_violation_raises_error(self):
        """Key range where key-from is greater than key-to raises ValueError."""
        with pytest.raises(ValueError, match="must be less than or equal to --key-to"):
            jira.build_jql_query(key_from="PROJ-200", key_to="PROJ-100")

    def test_key_from_only(self):
        query = jira.build_jql_query(key_from="PROJ-100")
        assert query == 'key >= "PROJ-100"'

    def test_key_to_only(self):
        query = jira.build_jql_query(key_to="PROJ-150")
        assert query == 'key <= "PROJ-150"'

    def test_key_range_different_projects_raises_error(self):
        """Key range with mismatched project prefixes raises ValueError."""
        with pytest.raises(ValueError, match="must belong to the same project"):
            jira.build_jql_query(key_from="PROJ1-100", key_to="PROJ2-150")

    def test_key_range_invalid_format_raises_error(self):
        """Invalid issue key formats raise ValueError."""
        with pytest.raises(ValueError, match="Invalid issue key format for --key-from"):
            jira.build_jql_query(key_from="INVALIDKEY")

        with pytest.raises(ValueError, match="Invalid issue key format for --key-to"):
            jira.build_jql_query(key_to="INVALIDKEY")

    def test_issue_type(self):
        """Issue type creates issuetype = 'TYPE' clause."""
        query = jira.build_jql_query(issue_type="Incident")
        assert query == 'issuetype = "Incident"'

    def test_status_single_and_multiple(self):
        """Single and multiple status values create appropriate JQL clauses."""
        q_single = jira.build_jql_query(status=["Done"])
        assert q_single == 'status = "Done"'

        q_multi = jira.build_jql_query(status=["Done", "Closed"])
        assert q_multi == 'status IN ("Done", "Closed")'

    def test_labels_single_and_multiple(self):
        """Single and multiple label values create appropriate JQL clauses."""
        q_single = jira.build_jql_query(labels=["alert-triage"])
        assert q_single == 'labels = "alert-triage"'

        q_multi = jira.build_jql_query(labels=["alert-triage", "temporary"])
        assert q_multi == 'labels IN ("alert-triage", "temporary")'

    def test_reporter_and_assignee(self):
        """Reporter and assignee generate escaped user clauses."""
        query = jira.build_jql_query(reporter="user@example.com", assignee="agent@example.com")
        assert 'reporter = "user@example.com"' in query
        assert 'assignee = "agent@example.com"' in query
        assert " AND " in query

    def test_summary_contains_single(self):
        """Single summary contains creates summary ~ 'TEXT' clause."""
        query = jira.build_jql_query(summary_contains=["database"])
        assert query == 'summary ~ "database"'

    def test_summary_contains_multiple(self):
        """Multiple summary contains entries are grouped with OR inside parens."""
        query = jira.build_jql_query(summary_contains=["database", "latency"])
        assert query == '(summary ~ "database" OR summary ~ "latency")'

    def test_raw_jql_wrapped_in_parens(self):
        """Raw JQL clause is wrapped in parentheses."""
        query = jira.build_jql_query(raw_jql='priority = "High"')
        assert query == '(priority = "High")'

    def test_combined_filters_with_and(self):
        """Multiple filters are combined together using AND."""
        query = jira.build_jql_query(
            projects=["PROJ"],
            older_than=7,
            status=["Done", "Closed"],
            labels=["temp"],
            raw_jql='priority = "High"',
        )
        assert 'project = "PROJ"' in query
        assert 'created <= "-7d"' in query
        assert 'status IN ("Done", "Closed")' in query
        assert 'labels = "temp"' in query
        assert '(priority = "High")' in query
        parts = query.split(" AND ")
        assert len(parts) == 5


# ---------------------------------------------------------------------------
# JQL Injection & Escaping Tests
# ---------------------------------------------------------------------------
class TestInjectionAndEscaping:
    def test_quote_injection_in_project_kept_literal(self):
        """JQL injection attempting to escape quotes remains a strict string literal."""
        malicious = '" OR project != "OTHER'
        query = jira.build_jql_query(projects=[malicious])
        expected = r'project = "\" OR project != \"OTHER"'
        assert query == expected

    def test_injection_in_summary_contains(self):
        """Injection attempt in summary_contains remains safely escaped inside quotes."""
        malicious = r'test" OR 1=1 --'
        query = jira.build_jql_query(summary_contains=[malicious])
        assert query == r'summary ~ "test\" OR 1=1 --"'

    def test_injection_in_reporter(self):
        """Injection in reporter is properly escaped."""
        malicious = 'admin" OR assignee IS NOT NULL OR "'
        query = jira.build_jql_query(reporter=malicious)
        assert query == r'reporter = "admin\" OR assignee IS NOT NULL OR \""'

    def test_backslash_escaping(self):
        """Backslashes are escaped so they cannot escape following characters."""
        value = r"C:\data\log"
        query = jira.build_jql_query(issue_type=value)
        assert query == r'issuetype = "C:\\data\\log"'

    @pytest.mark.parametrize(
        "kwargs,field_name",
        [
            ({"projects": ["PROJ\nINJECT"]}, "project"),
            ({"projects": ["PROJ\rINJECT"]}, "project"),
            ({"status": ["Done\nClosed"]}, "status"),
            ({"labels": ["tag\r"]}, "labels"),
            ({"reporter": "user\nname"}, "reporter"),
            ({"assignee": "agent\rname"}, "assignee"),
            ({"summary_contains": ["text\nmore"]}, "summary-contains"),
            ({"raw_jql": "project = PROJ\nAND 1=1"}, "jql"),
            ({"created_before": "2026-10-01\n"}, "datetime"),
            ({"created_after": "2026-10-01\r"}, "datetime"),
            ({"key_from": "PROJ-1\n"}, "key-from"),
            ({"key_to": "PROJ-2\r"}, "key-to"),
            ({"keys": ["PROJ-1\n2"]}, "keys"),
            ({"issue_type": "Bug\n"}, "issue-type"),
        ],
    )
    def test_rejection_of_newlines_and_carriage_returns(self, kwargs, field_name):
        """Any newline or carriage return character in filters raises ValueError."""
        with pytest.raises(ValueError, match="values must not contain newline or carriage return"):
            jira.build_jql_query(**kwargs)


# ---------------------------------------------------------------------------
# CLI Argument Parsing & Safety Tests
# ---------------------------------------------------------------------------
class TestArgumentParsing:
    @pytest.mark.parametrize("flag", ["--token", "--api-token", "--password"])
    def test_forbidden_secret_flags_rejected(self, flag):
        """CLI immediately rejects secret flags to protect shell history."""
        with pytest.raises(SystemExit) as exc_info:
            jira.parse_arguments([flag, "secret123", "--project", "PROJ"])
        assert "strictly forbidden to prevent credential exposure" in str(exc_info.value)

    @pytest.mark.parametrize("flag", ["--token=secret", "--api-token=secret", "--password=secret"])
    def test_forbidden_secret_flags_with_equals_rejected(self, flag):
        """CLI immediately rejects secret flags using = syntax."""
        with pytest.raises(SystemExit) as exc_info:
            jira.parse_arguments([flag, "--project", "PROJ"])
        assert "strictly forbidden to prevent credential exposure" in str(exc_info.value)

    def test_no_filters_specified_rejected(self):
        """Running with zero filters raises SystemExit to prevent accidental mass deletion."""
        with pytest.raises(SystemExit) as exc_info:
            jira.parse_arguments(["--site-url", "https://example.atlassian.net", "--email", "user@example.com"])
        assert "At least one filter must be specified" in str(exc_info.value)

    def test_limit_validation(self):
        """Invalid --limit values raise SystemExit."""
        with pytest.raises(SystemExit, match="--limit must be a positive integer"):
            jira.parse_arguments(["--project", "PROJ", "--limit", "0"])

        with pytest.raises(SystemExit, match="exceeds safety maximum ceiling"):
            jira.parse_arguments(["--project", "PROJ", "--limit", "20000", "--max-limit", "10000"])

    def test_flattening_comma_separated_arguments(self):
        """Comma-separated values for repeatable arguments are parsed into lists."""
        args = jira.parse_arguments([
            "--project", "PROJ1,PROJ2",
            "--keys", "PROJ-1,PROJ-2,PROJ-3",
            "--status", "Done,Closed",
            "--labels", "alert-triage,temp",
        ])
        assert args.project == ["PROJ1", "PROJ2"]
        assert args.keys == ["PROJ-1", "PROJ-2", "PROJ-3"]
        assert args.status == ["Done", "Closed"]
        assert args.labels == ["alert-triage", "temp"]


# ---------------------------------------------------------------------------
# Credential Resolution Tests
# ---------------------------------------------------------------------------
class TestCredentialResolution:
    def test_resolve_from_cli_and_env(self, monkeypatch):
        """Resolves site URL and email from CLI, token from env var."""
        monkeypatch.setenv("JIRA_API_TOKEN", "secret-token-xyz")
        url, email, token = jira.resolve_credentials(
            cli_site_url="https://myteam.atlassian.net",
            cli_email="admin@example.com",
            interactive=False,
        )
        assert url == "https://myteam.atlassian.net"
        assert email == "admin@example.com"
        assert token == "secret-token-xyz"

    def test_resolve_all_from_env(self, monkeypatch):
        """Resolves all credentials from environment variables."""
        monkeypatch.setenv("JIRA_SITE_URL", "https://envsite.atlassian.net")
        monkeypatch.setenv("JIRA_EMAIL", "env@example.com")
        monkeypatch.setenv("JIRA_API_TOKEN", "env-token-123")
        url, email, token = jira.resolve_credentials(None, None, interactive=False)
        assert url == "https://envsite.atlassian.net"
        assert email == "env@example.com"
        assert token == "env-token-123"

    def test_url_normalization(self, monkeypatch):
        """Normalizes URLs by adding https:// and stripping trailing slashes."""
        monkeypatch.setenv("JIRA_API_TOKEN", "token")
        url, _, _ = jira.resolve_credentials("myteam.atlassian.net/", "user@example.com", interactive=False)
        assert url == "https://myteam.atlassian.net"

    def test_missing_credentials_raises_error(self, monkeypatch):
        """Missing site URL, email, or token raises ValueError when non-interactive."""
        monkeypatch.delenv("JIRA_SITE_URL", raising=False)
        monkeypatch.delenv("JIRA_EMAIL", raising=False)
        monkeypatch.delenv("JIRA_API_TOKEN", raising=False)

        with pytest.raises(ValueError, match="Jira Site URL is required"):
            jira.resolve_credentials(None, "user@example.com", interactive=False)

        with pytest.raises(ValueError, match="Jira Account Email is required"):
            jira.resolve_credentials("https://test.atlassian.net", None, interactive=False)

        with pytest.raises(ValueError, match="Jira API Token is required"):
            jira.resolve_credentials("https://test.atlassian.net", "user@example.com", interactive=False)

    def test_interactive_getpass_prompt(self, monkeypatch):
        """When token is missing and stdin is a tty, prompts interactively via getpass."""
        monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
        with patch("sys.stdin.isatty", return_value=True), patch("getpass.getpass", return_value="prompted-token"):
            url, email, token = jira.resolve_credentials(
                "https://test.atlassian.net",
                "user@example.com",
                interactive=True,
            )
            assert token == "prompted-token"


# ---------------------------------------------------------------------------
# Sensitive Data Logging Masking Tests
# ---------------------------------------------------------------------------
class TestSensitiveDataLogging:
    def test_token_and_basic_auth_masked(self):
        """SensitiveDataFilter masks plain token strings, Basic auth headers, and token keywords."""
        secret_token = "ATATT3xFfGF0abc123secret"
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg=f"Calling API with token '{secret_token}', Header Authorization: Basic dXNlcjp0b2tlbg==, token=xyz999",
            args=(),
            exc_info=None,
        )
        data_filter = jira.SensitiveDataFilter(secrets_to_mask=[secret_token])
        data_filter.filter(record)

        assert secret_token not in record.msg
        assert "********" in record.msg
        assert "Basic ********" in record.msg
        assert "token=********" in record.msg


# ---------------------------------------------------------------------------
# Search & Pagination Tests
# ---------------------------------------------------------------------------
class TestSearchAndPagination:
    def test_single_page_search(self):
        """Searches POST /rest/api/3/search/jql and returns issue list."""
        mock_session = MagicMock(spec=jira.HttpSession)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "issues": [
                {
                    "key": "PROJ-1",
                    "fields": {
                        "summary": "Database latency alert",
                        "status": {"name": "Done"},
                        "issuetype": {"name": "Incident"},
                        "created": "2026-10-01T12:00:00.000+0000",
                        "project": {"key": "PROJ"},
                    },
                }
            ],
            "nextPageToken": None,
            "isLast": True,
        }
        mock_session.post.return_value = mock_response

        issues = jira.fetch_issues(
            session=mock_session,
            site_url="https://test.atlassian.net",
            jql='project = "PROJ"',
            limit=50,
        )

        assert len(issues) == 1
        assert issues[0]["key"] == "PROJ-1"
        mock_session.post.assert_called_once()
        call_url, call_kwargs = mock_session.post.call_args
        assert call_url[0] == "https://test.atlassian.net/rest/api/3/search/jql"
        assert call_kwargs["json"]["jql"] == 'project = "PROJ"'
        assert "fields" in call_kwargs["json"]

    def test_multi_page_pagination_via_next_page_token(self):
        """Follows nextPageToken across multiple pages until isLast=True."""
        mock_session = MagicMock(spec=jira.HttpSession)

        resp1 = MagicMock()
        resp1.status_code = 200
        resp1.json.return_value = {
            "issues": [{"key": "PROJ-1", "fields": {"summary": "Issue 1"}}],
            "nextPageToken": "token-page-2",
            "isLast": False,
        }

        resp2 = MagicMock()
        resp2.status_code = 200
        resp2.json.return_value = {
            "issues": [{"key": "PROJ-2", "fields": {"summary": "Issue 2"}}],
            "nextPageToken": None,
            "isLast": True,
        }

        mock_session.post.side_effect = [resp1, resp2]

        issues = jira.fetch_issues(
            session=mock_session,
            site_url="https://test.atlassian.net",
            jql='project = "PROJ"',
            limit=10,
            page_size=1,
        )

        assert len(issues) == 2
        assert [i["key"] for i in issues] == ["PROJ-1", "PROJ-2"]
        assert mock_session.post.call_count == 2
        # Check second call included nextPageToken
        second_call_payload = mock_session.post.call_args_list[1][1]["json"]
        assert second_call_payload.get("nextPageToken") == "token-page-2"

    def test_pagination_stops_at_limit(self):
        """Paging stops as soon as limit is reached even if more pages exist."""
        mock_session = MagicMock(spec=jira.HttpSession)
        resp1 = MagicMock()
        resp1.status_code = 200
        resp1.json.return_value = {
            "issues": [{"key": f"PROJ-{i}", "fields": {"summary": f"Issue {i}"}} for i in range(5)],
            "nextPageToken": "token-page-2",
            "isLast": False,
        }
        mock_session.post.return_value = resp1

        issues = jira.fetch_issues(
            session=mock_session,
            site_url="https://test.atlassian.net",
            jql='project = "PROJ"',
            limit=3,
            page_size=5,
        )
        assert len(issues) == 3
        assert mock_session.post.call_count == 1

    def test_client_side_summary_regex(self):
        """Filters issues client-side matching summary regex pattern."""
        mock_session = MagicMock(spec=jira.HttpSession)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "issues": [
                {"key": "PROJ-1", "fields": {"summary": "Memory leak detected"}},
                {"key": "PROJ-2", "fields": {"summary": "Disk space low"}},
                {"key": "PROJ-3", "fields": {"summary": "Out of memory error"}},
            ],
            "nextPageToken": None,
            "isLast": True,
        }
        mock_session.post.return_value = mock_response

        issues = jira.fetch_issues(
            session=mock_session,
            site_url="https://test.atlassian.net",
            jql='project = "PROJ"',
            summary_regex=r"(?i)memory",
        )
        assert len(issues) == 2
        assert [i["key"] for i in issues] == ["PROJ-1", "PROJ-3"]


# ---------------------------------------------------------------------------
# Delete & Retry Tests
# ---------------------------------------------------------------------------
class TestDeleteAndRetry:
    def test_successful_deletion(self):
        """DELETE request sends deleteSubtasks=true and succeeds on HTTP 204."""
        mock_session = MagicMock(spec=jira.HttpSession)
        mock_resp = MagicMock()
        mock_resp.status_code = 204
        mock_session.delete.return_value = mock_resp

        issues = [{"key": "PROJ-101"}, {"key": "PROJ-102"}]
        successful, failures = jira.delete_issues(
            session=mock_session,
            site_url="https://test.atlassian.net",
            issues=issues,
        )

        assert successful == ["PROJ-101", "PROJ-102"]
        assert failures == []
        assert mock_session.delete.call_count == 2
        first_call_url = mock_session.delete.call_args_list[0][0][0]
        assert first_call_url == "https://test.atlassian.net/rest/api/3/issue/PROJ-101?deleteSubtasks=true"

    def test_http_404_treated_as_already_deleted(self):
        """HTTP 404 is treated as successfully removed."""
        mock_session = MagicMock(spec=jira.HttpSession)
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_session.delete.return_value = mock_resp

        issues = [{"key": "PROJ-101"}]
        successful, failures = jira.delete_issues(
            session=mock_session,
            site_url="https://test.atlassian.net",
            issues=issues,
        )
        assert successful == ["PROJ-101"]
        assert failures == []

    def test_rate_limiting_429_honors_retry_after(self):
        """HTTP 429 parses Retry-After header and sleeps for specified seconds plus jitter."""
        mock_session = MagicMock(spec=jira.HttpSession)
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.text = "Rate limit exceeded"
        resp_429.headers = {"Retry-After": "2"}

        resp_204 = MagicMock()
        resp_204.status_code = 204

        mock_session.delete.side_effect = [resp_429, resp_204]

        with patch("time.sleep") as mock_sleep:
            successful, failures = jira.delete_issues(
                session=mock_session,
                site_url="https://test.atlassian.net",
                issues=[{"key": "PROJ-101"}],
                max_retries=3,
            )

        assert successful == ["PROJ-101"]
        assert failures == []
        assert mock_sleep.call_count == 1
        slept = mock_sleep.call_args[0][0]
        assert 2.0 <= slept <= 2.5

    def test_transient_500_retries_with_exponential_backoff_and_jitter(self):
        """HTTP 500 triggers exponential backoff retry with jitter."""
        mock_session = MagicMock(spec=jira.HttpSession)
        resp_500 = MagicMock()
        resp_500.status_code = 500
        resp_500.text = "Internal Server Error"

        resp_200 = MagicMock()
        resp_200.status_code = 200

        mock_session.delete.side_effect = [resp_500, resp_200]

        with patch("time.sleep") as mock_sleep:
            successful, failures = jira.delete_issues(
                session=mock_session,
                site_url="https://test.atlassian.net",
                issues=[{"key": "PROJ-101"}],
                initial_backoff=1.5,
                backoff_factor=2.0,
                max_retries=3,
            )

        assert successful == ["PROJ-101"]
        assert failures == []
        assert mock_sleep.call_count == 1
        slept = mock_sleep.call_args[0][0]
        assert 1.5 <= slept <= 1.5 * 1.25

    def test_retry_without_jitter_deterministic(self):
        """When jitter is disabled, sleep time matches backoff exactly."""
        mock_session = MagicMock(spec=jira.HttpSession)
        resp_500 = MagicMock()
        resp_500.status_code = 500
        resp_500.text = "Internal Server Error"

        resp_200 = MagicMock()
        resp_200.status_code = 200

        mock_session.delete.side_effect = [resp_500, resp_200]

        with patch("time.sleep") as mock_sleep:
            successful, failures = jira.delete_issues(
                session=mock_session,
                site_url="https://test.atlassian.net",
                issues=[{"key": "PROJ-101"}],
                initial_backoff=1.5,
                jitter=False,
                max_retries=3,
            )

        assert successful == ["PROJ-101"]
        assert failures == []
        mock_sleep.assert_called_once_with(1.5)

    def test_missing_or_blank_key_skipped_without_delete(self):
        """Records with missing or empty key are recorded as failure without calling DELETE."""
        mock_session = MagicMock(spec=jira.HttpSession)

        issues = [
            {"key": ""},
            {"key": "   "},
            {"fields": {"summary": "No key at all"}},
        ]
        successful, failures = jira.delete_issues(
            session=mock_session,
            site_url="https://test.atlassian.net",
            issues=issues,
        )

        assert successful == []
        assert len(failures) == 3
        mock_session.delete.assert_not_called()

    def test_max_retries_exceeded_recorded_in_failures(self):
        """Exhausting max retries records failure with last error message."""
        mock_session = MagicMock(spec=jira.HttpSession)
        resp_503 = MagicMock()
        resp_503.status_code = 503
        resp_503.text = "Service Unavailable"
        mock_session.delete.return_value = resp_503

        with patch("time.sleep"):
            successful, failures = jira.delete_issues(
                session=mock_session,
                site_url="https://test.atlassian.net",
                issues=[{"key": "PROJ-101"}],
                max_retries=2,
            )

        assert successful == []
        assert len(failures) == 1
        assert failures[0]["key"] == "PROJ-101"
        assert "HTTP 503" in failures[0]["error"]


# ---------------------------------------------------------------------------
# Dry-Run & Apply Execution Tests
# ---------------------------------------------------------------------------
class TestDryRunAndApply:
    def test_render_table(self):
        """Table renderer formats issues into aligned text table."""
        issues = [
            {
                "key": "PROJ-1",
                "fields": {
                    "project": {"key": "PROJ"},
                    "issuetype": {"name": "Incident"},
                    "status": {"name": "Done"},
                    "created": "2026-10-01T12:00:00.000+0000",
                    "summary": "Database connectivity alert",
                },
            }
        ]
        table = jira.render_table(issues)
        assert "PROJ-1" in table
        assert "Incident" in table
        assert "Done" in table
        assert "Database connectivity alert" in table

    def test_render_csv(self):
        """CSV renderer outputs valid CSV with headers."""
        issues = [
            {
                "key": "PROJ-1",
                "fields": {
                    "project": {"key": "PROJ"},
                    "issuetype": {"name": "Incident"},
                    "status": {"name": "Done"},
                    "created": "2026-10-01",
                    "reporter": {"displayName": "Alice Admin"},
                    "summary": "High latency on orders-api",
                },
            }
        ]
        csv_out = jira.render_csv(issues)
        assert "key,project,issue_type,status,created,reporter,summary" in csv_out
        assert "PROJ-1,PROJ,Incident,Done,2026-10-01,Alice Admin,High latency on orders-api" in csv_out

    def test_render_json(self):
        """JSON renderer outputs valid JSON matching issue records."""
        issues = [{"key": "PROJ-1", "fields": {"summary": "Alert 1"}}]
        json_out = jira.render_json(issues)
        parsed = json.loads(json_out)
        assert parsed[0]["key"] == "PROJ-1"

    @patch("urllib.request.urlopen")
    def test_dry_run_executes_zero_deletes(self, mock_urlopen, capsys, monkeypatch):
        """Dry-run queries issues and executes zero DELETE requests."""
        monkeypatch.setenv("JIRA_API_TOKEN", "mock-token")
        search_resp = make_mock_response(
            status=200,
            json_data={"issues": [{"key": "PROJ-10", "fields": {"summary": "Alert"}}]},
        )
        mock_urlopen.return_value = search_resp

        exit_code = jira.main([
            "--site-url", "https://test.atlassian.net",
            "--email", "user@example.com",
            "--project", "PROJ",
        ])

        assert exit_code == 0
        assert not any(c[0][0].method == "DELETE" for c in mock_urlopen.call_args_list)
        captured = capsys.readouterr()
        assert "[DRY-RUN MODE] Found 1 matching issue(s). Zero deletions executed." in captured.out

    @patch("urllib.request.urlopen")
    def test_apply_confirmation_mismatch_aborts_without_deleting(
        self, mock_urlopen, capsys, monkeypatch
    ):
        """Apply mode aborts with 0 deletions when interactive count confirmation fails."""
        monkeypatch.setenv("JIRA_API_TOKEN", "mock-token")
        search_resp = make_mock_response(
            status=200,
            json_data={"issues": [{"key": "PROJ-10", "fields": {"summary": "Alert"}}]},
        )
        mock_urlopen.return_value = search_resp

        # Operator inputs incorrect count
        with patch("builtins.input", return_value="999"):
            exit_code = jira.main([
                "--site-url", "https://test.atlassian.net",
                "--email", "user@example.com",
                "--project", "PROJ",
                "--apply",
            ])

        assert exit_code == 1
        assert not any(c[0][0].method == "DELETE" for c in mock_urlopen.call_args_list)
        captured = capsys.readouterr()
        assert "Aborted: Confirmation '999' did not match issue count '1'." in captured.out

    @patch("urllib.request.urlopen")
    def test_apply_with_exact_count_confirmation_succeeds(
        self, mock_urlopen, capsys, monkeypatch
    ):
        """Apply mode succeeds when operator types matching count."""
        monkeypatch.setenv("JIRA_API_TOKEN", "mock-token")
        search_resp = make_mock_response(
            status=200,
            json_data={"issues": [{"key": "PROJ-10", "fields": {"summary": "Alert"}}]},
        )
        del_resp = make_mock_response(status=204)
        mock_urlopen.side_effect = [search_resp, del_resp]

        with patch("builtins.input", return_value="1"):
            exit_code = jira.main([
                "--site-url", "https://test.atlassian.net",
                "--email", "user@example.com",
                "--project", "PROJ",
                "--apply",
            ])

        assert exit_code == 0
        del_calls = [c for c in mock_urlopen.call_args_list if c[0][0].method == "DELETE"]
        assert len(del_calls) == 1
        assert "deleteSubtasks=true" in del_calls[0][0][0].full_url
        captured = capsys.readouterr()
        assert "Successfully deleted: 1" in captured.out

    @patch("urllib.request.urlopen")
    def test_apply_with_yes_flag_skips_prompt(
        self, mock_urlopen, capsys, monkeypatch
    ):
        """Apply mode with --yes echoes count and executes deletions without prompting."""
        monkeypatch.setenv("JIRA_API_TOKEN", "mock-token")
        search_resp = make_mock_response(
            status=200,
            json_data={
                "issues": [
                    {"key": "PROJ-1", "fields": {"summary": "Issue 1"}},
                    {"key": "PROJ-2", "fields": {"summary": "Issue 2"}},
                ]
            },
        )
        del_resp1 = make_mock_response(status=204)
        del_resp2 = make_mock_response(status=204)
        mock_urlopen.side_effect = [search_resp, del_resp1, del_resp2]

        with patch("builtins.input") as mock_input:
            exit_code = jira.main([
                "--site-url", "https://test.atlassian.net",
                "--email", "user@example.com",
                "--project", "PROJ",
                "--apply",
                "--yes",
            ])

        mock_input.assert_not_called()
        assert exit_code == 0
        del_calls = [c for c in mock_urlopen.call_args_list if c[0][0].method == "DELETE"]
        assert len(del_calls) == 2
        captured = capsys.readouterr()
        assert "Confirming deletion of 2 issues (--yes specified)." in captured.out
        assert "Successfully deleted: 2" in captured.out

    @patch("urllib.request.urlopen")
    def test_warning_when_project_omitted(self, mock_urlopen, capsys, monkeypatch):
        """When --project is omitted, a loud warning is emitted to stderr."""
        monkeypatch.setenv("JIRA_API_TOKEN", "mock-token")
        search_resp = make_mock_response(status=200, json_data={"issues": []})
        mock_urlopen.return_value = search_resp

        exit_code = jira.main([
            "--site-url", "https://test.atlassian.net",
            "--email", "user@example.com",
            "--older-than", "30",
        ])

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "WARNING: No --project filter specified!" in captured.err
        assert "Zero deletions executed." in captured.out

    @patch("urllib.request.urlopen")
    def test_zero_matching_issues_in_apply_mode(self, mock_urlopen, capsys, monkeypatch):
        """When apply mode finds 0 matching issues, exits cleanly without error."""
        monkeypatch.setenv("JIRA_API_TOKEN", "mock-token")
        search_resp = make_mock_response(status=200, json_data={"issues": []})
        mock_urlopen.return_value = search_resp

        exit_code = jira.main([
            "--site-url", "https://test.atlassian.net",
            "--email", "user@example.com",
            "--project", "PROJ",
            "--apply",
        ])

        assert exit_code == 0
        assert not any(c[0][0].method == "DELETE" for c in mock_urlopen.call_args_list)
        captured = capsys.readouterr()
        assert "No matching issues found. Nothing to delete." in captured.out

    def test_network_exception_retry_and_recovery(self):
        """Network RequestException triggers retry and eventual success."""
        mock_session = MagicMock(spec=jira.HttpSession)
        req_exc = jira.RequestError("Connection reset by peer")
        success_resp = MagicMock()
        success_resp.status_code = 204

        mock_session.delete.side_effect = [req_exc, success_resp]

        with patch("time.sleep"):
            successful, failures = jira.delete_issues(
                session=mock_session,
                site_url="https://test.atlassian.net",
                issues=[{"key": "PROJ-101"}],
                max_retries=2,
            )

        assert successful == ["PROJ-101"]
        assert failures == []
        assert mock_session.delete.call_count == 2

    def test_retry_after_invalid_header_falls_back(self):
        """Non-numeric Retry-After header falls back to default backoff."""
        mock_session = MagicMock(spec=jira.HttpSession)
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.text = "Too many requests"
        resp_429.headers = {"Retry-After": "invalid-timestamp"}

        resp_200 = MagicMock()
        resp_200.status_code = 200

        mock_session.delete.side_effect = [resp_429, resp_200]

        with patch("time.sleep") as mock_sleep:
            successful, failures = jira.delete_issues(
                session=mock_session,
                site_url="https://test.atlassian.net",
                issues=[{"key": "PROJ-101"}],
                initial_backoff=1.2,
                max_retries=2,
            )

        assert successful == ["PROJ-101"]
        assert failures == []
        assert mock_sleep.call_count == 1
        slept = mock_sleep.call_args[0][0]
        assert 1.2 <= slept <= 1.2 * 1.25


# ---------------------------------------------------------------------------
# HTTP Client Helper Tests
# ---------------------------------------------------------------------------
class TestHttpSession:
    def test_basic_auth_header_encoding(self):
        """Basic auth header is correctly base64-encoded."""
        session = jira.HttpSession()
        session.set_basic_auth("user@example.com", "api_token_123")
        assert session.headers["Authorization"] == "Basic dXNlckBleGFtcGxlLmNvbTphcGlfdG9rZW5fMTIz"

    @patch("urllib.request.urlopen")
    def test_post_with_json_and_tls_context(self, mock_urlopen):
        """POST request serializes JSON payload and enforces TLS default context for https."""
        mock_urlopen.return_value = make_mock_response(status=200, json_data={"issues": []})
        session = jira.HttpSession()
        session.set_basic_auth("user@example.com", "token")
        resp = session.post("https://test.atlassian.net/rest/api/3/search/jql", json={"jql": "project = PROJ"})

        assert resp.status_code == 200
        assert resp.json() == {"issues": []}
        assert mock_urlopen.call_count == 1
        req = mock_urlopen.call_args[0][0]
        context = mock_urlopen.call_args[1].get("context")
        assert context is not None
        assert req.method == "POST"
        assert json.loads(req.data.decode("utf-8")) == {"jql": "project = PROJ"}
        assert req.headers["Content-type"] == "application/json"
        assert req.headers["Authorization"] == "Basic dXNlckBleGFtcGxlLmNvbTp0b2tlbg=="

    @patch("urllib.request.urlopen")
    def test_http_error_preserves_status_code_and_retry_after(self, mock_urlopen):
        """urllib.error.HTTPError preserves HTTP status code, headers, and body."""
        http_err = urllib.error.HTTPError(
            "https://test.atlassian.net/rest/api/3/issue/PROJ-1",
            429,
            "Too Many Requests",
            {"Retry-After": "3", "Content-Type": "application/json"},
            io.BytesIO(b'{"error": "rate limited"}'),
        )
        mock_urlopen.side_effect = http_err

        session = jira.HttpSession()
        resp = session.delete("https://test.atlassian.net/rest/api/3/issue/PROJ-1")
        assert resp.status_code == 429
        assert resp.headers.get("Retry-After") == "3"
        assert resp.text == '{"error": "rate limited"}'

        with pytest.raises(jira.HTTPStatusError, match="HTTP 429"):
            resp.raise_for_status()

    @patch("urllib.request.urlopen")
    def test_network_error_raises_request_error(self, mock_urlopen):
        """urllib.error.URLError raises RequestError."""
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
        session = jira.HttpSession()
        with pytest.raises(jira.RequestError, match="Network error: Connection refused"):
            session.post("https://test.atlassian.net/rest/api/3/search/jql", json={})

    @patch("urllib.request.urlopen")
    def test_https_scheme_case_insensitive(self, mock_urlopen):
        """TLS verification context is created for HTTPS scheme regardless of casing."""
        mock_urlopen.return_value = make_mock_response(status=200, json_data={"issues": []})
        session = jira.HttpSession()

        session.get("HTTPS://test.atlassian.net/rest/api/3/search/jql")
        context_upper = mock_urlopen.call_args[1].get("context")
        assert context_upper is not None

        session.get("Https://test.atlassian.net/rest/api/3/search/jql")
        context_mixed = mock_urlopen.call_args[1].get("context")
        assert context_mixed is not None

        session.get("http://test.atlassian.net/rest/api/3/search/jql")
        context_http = mock_urlopen.call_args[1].get("context")
        assert context_http is None


class TestTLSCertificateHandling:
    """Tests for CA bundle resolution, TLS verification enforcement, and cert verification error hints in Jira cleanup."""

    def test_cafile_precedence_flag_over_env_and_certifi(self, monkeypatch):
        """CLI flag takes precedence over environment variables and certifi."""
        monkeypatch.setenv("SSL_CERT_FILE", "/env/ssl_cert.pem")
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/env/requests_ca.pem")
        mock_certifi = types.ModuleType("certifi")
        mock_certifi.where = lambda: "/certifi/where.pem"
        monkeypatch.setitem(sys.modules, "certifi", mock_certifi)

        resolved = jira.resolve_ca_bundle(ca_bundle="/flag/custom_ca.pem")
        assert resolved == "/flag/custom_ca.pem"

    def test_cafile_precedence_env_ssl_cert_file_over_requests_ca_bundle_and_certifi(self, monkeypatch):
        """SSL_CERT_FILE takes precedence over REQUESTS_CA_BUNDLE and certifi when no flag is given."""
        monkeypatch.setenv("SSL_CERT_FILE", "/env/ssl_cert.pem")
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/env/requests_ca.pem")
        mock_certifi = types.ModuleType("certifi")
        mock_certifi.where = lambda: "/certifi/where.pem"
        monkeypatch.setitem(sys.modules, "certifi", mock_certifi)

        resolved = jira.resolve_ca_bundle(ca_bundle=None)
        assert resolved == "/env/ssl_cert.pem"

    def test_cafile_precedence_env_requests_ca_bundle_over_certifi(self, monkeypatch):
        """REQUESTS_CA_BUNDLE takes precedence over certifi when SSL_CERT_FILE is unset."""
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/env/requests_ca.pem")
        mock_certifi = types.ModuleType("certifi")
        mock_certifi.where = lambda: "/certifi/where.pem"
        monkeypatch.setitem(sys.modules, "certifi", mock_certifi)

        resolved = jira.resolve_ca_bundle(ca_bundle=None)
        assert resolved == "/env/requests_ca.pem"

    def test_cafile_precedence_certifi_over_default(self, monkeypatch):
        """certifi.where() is used when no flag or environment variables are provided."""
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
        mock_certifi = types.ModuleType("certifi")
        mock_certifi.where = lambda: "/certifi/where.pem"
        monkeypatch.setitem(sys.modules, "certifi", mock_certifi)

        resolved = jira.resolve_ca_bundle(ca_bundle=None)
        assert resolved == "/certifi/where.pem"

    def test_cafile_precedence_default_when_no_certifi(self, monkeypatch):
        """Returns None (system default) when no flag, no env, and certifi is not importable."""
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
        monkeypatch.setitem(sys.modules, "certifi", None)

        resolved = jira.resolve_ca_bundle(ca_bundle=None)
        assert resolved is None

    @patch("ssl.create_default_context")
    def test_create_ssl_context_calls_create_default_context_with_cafile(self, mock_create_ctx):
        """create_ssl_context resolves bundle and passes cafile to ssl.create_default_context."""
        mock_ctx = MagicMock()
        mock_create_ctx.return_value = mock_ctx

        ctx = jira.create_ssl_context(ca_bundle="/path/to/my_bundle.pem")
        mock_create_ctx.assert_called_once_with(cafile="/path/to/my_bundle.pem")
        assert ctx == mock_ctx

    def test_tls_verification_never_disabled(self):
        """TLS certificate verification and hostname checking are never disabled."""
        ctx_default = jira.create_ssl_context(ca_bundle=None)
        assert ctx_default.verify_mode == ssl.CERT_REQUIRED
        assert ctx_default.check_hostname is True

        # Insecure bypass flags must NOT exist
        parser = jira.build_arg_parser()
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
        req_err = jira.RequestError(f"Network error: {url_err.reason}")
        req_err.__cause__ = url_err

        assert jira.is_cert_verification_error(cert_err) is True
        assert jira.is_cert_verification_error(url_err) is True
        assert jira.is_cert_verification_error(req_err) is True
        assert jira.is_cert_verification_error(jira.RequestError("Network error: [SSL: CERTIFICATE_VERIFY_FAILED] ...")) is True

        # Non-verification errors should return False
        assert jira.is_cert_verification_error(jira.RequestError("Network error: Connection refused")) is False
        assert jira.is_cert_verification_error(urllib.error.URLError("Connection reset")) is False
        assert jira.is_cert_verification_error(ValueError("Invalid argument")) is False

    @patch("urllib.request.urlopen")
    def test_cert_verification_failure_in_main_prints_hint_and_no_traceback(self, mock_urlopen, capsys, monkeypatch):
        """main exits non-zero and prints macOS/certifi/ca-bundle hint with no traceback on CERTIFICATE_VERIFY_FAILED."""
        monkeypatch.setenv("JIRA_API_TOKEN", "token123")
        cert_err = ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed (_ssl.c:1029)")
        mock_urlopen.side_effect = urllib.error.URLError(cert_err)

        exit_code = jira.main([
            "--site-url", "https://test.atlassian.net",
            "--email", "test@example.com",
            "--project", "PROJ",
            "--older-than", "10",
        ])

        assert exit_code == 1
        captured = capsys.readouterr()
        err_out = captured.err

        # Check required hint contents
        assert "Install Certificates.command" in err_out
        assert "pip install certifi" in err_out
        assert "--ca-bundle" in err_out
        assert "SSL_CERT_FILE" in err_out

        # Verify NO traceback is output
        assert "Traceback (most recent call last)" not in err_out
        assert "Traceback (most recent call last)" not in captured.out

    def test_missing_ca_bundle_file_in_main_exits_nonzero(self, capsys):
        """Specifying a nonexistent CA bundle file exits non-zero with a clear error."""
        exit_code = jira.main([
            "--site-url", "https://test.atlassian.net",
            "--email", "test@example.com",
            "--project", "PROJ",
            "--ca-bundle", "/path/to/definitely_nonexistent_bundle.pem",
        ])
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "Configuration Error: CA bundle file not found" in captured.err

    @patch("urllib.request.urlopen")
    def test_cert_verification_failure_in_delete_prints_hint(self, mock_urlopen, capsys):
        """delete_issues detects cert failure, prints hint, and aborts immediately without retrying."""
        cert_err = ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
        mock_urlopen.side_effect = urllib.error.URLError(cert_err)

        session = jira.HttpSession()
        successful, failures = jira.delete_issues(
            session=session,
            site_url="https://test.atlassian.net",
            issues=[{"key": "PROJ-101"}],
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
        monkeypatch.setenv("JIRA_API_TOKEN", "token123")
        fake_ca = tmp_path / "custom_ca.pem"
        fake_ca.write_text("-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----\n")

        session_instances = []
        original_init = jira.HttpSession.__init__

        def mock_session_init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            session_instances.append(self)

        monkeypatch.setattr(jira.HttpSession, "__init__", mock_session_init)
        monkeypatch.setattr(jira, "fetch_issues", lambda *args, **kwargs: [])

        exit_code = jira.main([
            "--site-url", "https://test.atlassian.net",
            "--email", "test@example.com",
            "--project", "PROJ",
            "--ca-bundle", str(fake_ca),
        ])

        assert exit_code == 0
        assert len(session_instances) == 1
        assert session_instances[0].ca_bundle == str(fake_ca)


