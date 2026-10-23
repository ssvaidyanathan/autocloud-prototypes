# Scripts

Repo-wide utilities shared across prototypes. Each script is documented below.

| Script | Purpose |
|---|---|
| [`check-portable-sed.sh`](check-portable-sed.sh) | CI guard that blocks non-portable in-place `sed` usage (see the root README's Shell Script Portability guideline). |
| [`servicenow-cleanup.py`](servicenow-cleanup.py) | List and delete ServiceNow incidents (e.g. test/demo tickets) with filters; dry-run by default. |
| [`jira-cleanup.py`](jira-cleanup.py) | List and delete Jira Cloud issues (e.g. test/demo issues) with JQL-safe filters; dry-run by default. |

**Requirements:** Python 3.9+, no third-party packages (pytest only for running the tests).

This section documents repo-wide standalone administration and maintenance utilities located under `scripts/`.

## ServiceNow Incident Cleanup Utility (`scripts/servicenow-cleanup.py`)

A cross-prototype standalone administration tool to inspect and safely purge incident records from ServiceNow.

### Operational Model & Defaults

1. **Dry-Run by Default:**
   Running without `--apply` queries matching incident records and displays a formatted summary table (or CSV/JSON export) without issuing any `DELETE` calls.

2. **Strict Count Confirmation on Apply:**
   When running with `--apply`, the tool requires the operator to type the exact number of matching incidents to confirm execution. For scripted or CI/CD usage, `--yes` / `-y` can be provided to bypass interactive confirmation (the targeted count will still be echoed to the console).

3. **Cascading Child Deletion:**
   When an incident is deleted via the ServiceNow Table API (`DELETE /api/now/table/incident/{sys_id}`), ServiceNow database cascade rules automatically delete all child attachments in `sys_attachment` and associated audit/work-note entries in `sys_journal_field`.

4. **ServiceNow Role Requirements:**
   The credentials supplied must have `delete` privileges on the `incident` table. In standard ServiceNow instances, this requires `itil_admin`, `admin`, or a custom Access Control (ACL) granting delete permissions on `incident`.

5. **Filter Guardrail:**
   At least one filter criterion is required. If no filter is specified, `--older-than 7` is applied as a default. Multiple filters are combined with logical `AND` (`^`).

### CLI Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `--instance-url` | string | `SNOW_INSTANCE_URL` | ServiceNow instance base URL (e.g. `https://dev12345.service-now.com`). |
| `--username` | string | `SNOW_USERNAME` | ServiceNow username or service account. |
| `--table` | string | `incident` | Target table (strictly locked to `incident`). |
| `--older-than` | integer | 7 (if no other filter) | Incidents created more than N days ago (`sys_created_on<javascript:gs.daysAgoStart(N)`). |
| `--created-before` | ISO string | None | Incidents created before date/datetime (UTC, e.g. `2026-10-01` or `2026-10-01T12:00:00Z`). |
| `--created-after` | ISO string | None | Incidents created after date/datetime (UTC, e.g. `2026-10-01`). |
| `--number-from` | string | None | Lower bound of incident range (e.g. `INC0010000`). |
| `--number-to` | string | None | Upper bound of incident range (e.g. `INC0010500`). |
| `--numbers` | comma-list | None | Explicit incident numbers (e.g. `INC0010222,INC0010223`). |
| `--caller` | string | None | Caller username, full name, or 32-character sys_id. |
| `--short-description-contains` | string (repeatable) | None | Substring match on short description (matches any provided). |
| `--short-description-regex` | regex | None | Client-side regular expression filter on short description. |
| `--state` | string (repeatable) | None | Incident states: `new`, `in_progress`, `on_hold`, `resolved`, `closed`, `canceled`. |
| `--priority` | integer (repeatable)| None | Priority levels 1 through 5. |
| `--assignment-group` | string | None | Assignment group name or sys_id. |
| `--category` | string | None | Incident category. |
| `--created-by` | string | None | Filter by `sys_created_by` username (e.g. integration service account). |
| `--query` | string | None | Advanced ServiceNow encoded query string (ANDed with other filters). |
| `--apply` | flag | `False` | Execute deletion (default is dry-run mode). |
| `--yes`, `-y` | flag | `False` | Skip confirmation prompt when applying deletions. |
| `--limit` | integer | 500 | Safety cap on number of incidents to process (max ceiling 10,000). |
| `--output` | choice | `table` | Dry-run display format: `table`, `csv`, or `json`. |
| `--page-size` | integer | 100 | Batch paging size for Table API requests. |
| `--verbose`, `-v` | flag | `False` | Enable detailed debug logging. |

### Usage Examples

```bash
# 1. Default dry-run: audit tickets older than 7 days
python scripts/servicenow-cleanup.py \
  --instance-url https://dev12345.service-now.com \
  --username admin

# 2. Dry-run export to CSV for resolved and closed tickets older than 14 days
python scripts/servicenow-cleanup.py \
  --older-than 14 \
  --state resolved,closed \
  --output csv > stale_incidents.csv

# 3. Interactive apply: prompts to enter exact incident count before deleting
python scripts/servicenow-cleanup.py \
  --created-by svc_integration \
  --created-before 2026-10-15 \
  --apply

# 4. Non-interactive apply with --yes for explicit ticket cleanup
export SNOW_INSTANCE_URL="https://dev12345.service-now.com"
export SNOW_USERNAME="admin"
export SNOW_PASSWORD="<credentials-from-secure-store>"

python scripts/servicenow-cleanup.py \
  --numbers INC0010222,INC0010223,INC0010224 \
  --apply \
  --yes
```

## Jira Cloud Incident and Issue Cleanup Utility (`scripts/jira-cleanup.py`)

A standalone administration utility to inspect (dry-run) and safely purge incident and issue records from Jira Cloud.

| Utility | Target | Primary Use | Auth / Credentials |
|---|---|---|---|
| `scripts/jira-cleanup.py` | Jira Cloud REST API v3 | Audit & purge stale demo/test issues | `--site-url`, `--email`, `JIRA_API_TOKEN` |

### Operational Model & Defaults

1. **Dry-Run by Default:**
   Executing without `--apply` queries matching issues via `POST /rest/api/3/search/jql` and displays a formatted summary table (or CSV/JSON export). Zero `DELETE` calls are made.

2. **Strict Count Confirmation on Apply:**
   When running with `--apply`, the tool requires the operator to type the exact number of matching issues to confirm execution. For non-interactive automation, `--yes` / `-y` can be passed to bypass manual confirmation (the targeted count is echoed to the terminal).

3. **Cascading Child Subtask Deletion:**
   When an issue is deleted via the Jira Cloud REST API v3 endpoint (`DELETE /rest/api/3/issue/{key}?deleteSubtasks=true`), child subtasks are automatically removed.

4. **Required Jira Permissions:**
   The authenticated Jira user or service account must possess the following project permissions in Jira's permission scheme:
   - **Browse projects**: Required to query and view issues within the target projects.
   - **Delete issues**: Required to permanently remove issues and their subtasks.

5. **Filter Guardrail:**
   At least one filter criterion is mandatory to prevent accidental mass deletion. Providing `--project KEY` is strongly recommended; omitting `--project` triggers a prominent warning. Multiple filters are combined with logical `AND`.

### CLI Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `--site-url` | string | `JIRA_SITE_URL` | Jira Cloud site URL (e.g. `https://myteam.atlassian.net`). |
| `--email` | string | `JIRA_EMAIL` | Jira user email address associated with the API token. |
| `--project` | string (repeatable) | None | Project key (e.g. `PROJ`). Repeatable or comma-separated. Strongly recommended. |
| `--older-than` | integer | None | Filter issues created more than N days ago (`created <= "-Nd"`). |
| `--created-before` | ISO string | None | Filter issues created before UTC date/datetime (e.g. `2026-10-01` or `2026-10-01T12:00:00Z`). |
| `--created-after` | ISO string | None | Filter issues created after UTC date/datetime (e.g. `2026-10-01`). |
| `--keys` | comma-list | None | Explicit issue keys to target (e.g. `PROJ-101,PROJ-102`). |
| `--key-from` | string | None | Start of issue key range in the same project in numeric order (e.g. `PROJ-100`). |
| `--key-to` | string | None | End of issue key range in the same project in numeric order (e.g. `PROJ-200`; must be >= `--key-from`). |
| `--issue-type` | string | None | Filter by issue type name (e.g. `Incident`, `Bug`, `Task`). |
| `--status` | string (repeatable) | None | Filter by status name (e.g. `Done`, `Closed`). Repeatable or comma-separated. |
| `--labels` | string (repeatable) | None | Filter by label (e.g. `alert-triage`). Repeatable or comma-separated. |
| `--reporter` | string | None | Filter by reporter account ID, email, or username. |
| `--assignee` | string | None | Filter by assignee account ID, email, or username. |
| `--summary-contains` | string (repeatable) | None | Filter issues containing text in summary (combined with OR). |
| `--summary-regex` | regex | None | Client-side regular expression applied to issue summary. |
| `--jql` | string | None | Raw JQL clause combined with other filters using `AND`. |
| `--apply` | flag | `False` | Execute deletion (default is dry-run mode). |
| `--yes`, `-y` | flag | `False` | Skip interactive count confirmation during apply mode. |
| `--limit` | integer | 500 | Safety cap on number of issues processed (ceiling: `--max-limit`). |
| `--max-limit` | integer | 10000 | Configurable upper bound safety ceiling. |
| `--output` | choice | `table` | Display format: `table`, `csv`, or `json`. |
| `--page-size` | integer | 100 | Number of issues requested per search request. |
| `--verbose`, `-v` | flag | `False` | Enable detailed debug logging. |

### Usage Examples

```bash
# 1. Project-scoped dry-run for issues older than 14 days
python scripts/jira-cleanup.py \
  --site-url https://myteam.atlassian.net \
  --email operator@example.com \
  --project PROJ \
  --older-than 14

# 2. Dry-run export to CSV for resolved and closed incidents
python scripts/jira-cleanup.py \
  --project PROJ \
  --issue-type Incident \
  --status Done,Closed \
  --output csv > stale_incidents.csv

# 3. Target a specific key range within a project
python scripts/jira-cleanup.py \
  --key-from PROJ-100 \
  --key-to PROJ-150 \
  --status Done

# 4. Interactive apply deletion (requires typing exact issue count to confirm)
python scripts/jira-cleanup.py \
  --project PROJ \
  --labels alert-triage \
  --older-than 30 \
  --apply

# 5. Non-interactive apply with environment variables and --yes
export JIRA_SITE_URL="https://myteam.atlassian.net"
export JIRA_EMAIL="operator@example.com"
export JIRA_API_TOKEN="<api-token-from-secure-store>"

python scripts/jira-cleanup.py \
  --project PROJ \
  --keys PROJ-501,PROJ-502,PROJ-503 \
  --apply \
  --yes
```
