# Scripts

Repo-wide utilities shared across prototypes. Each script is documented below.

| Script | Purpose |
|---|---|
| [`check-portable-sed.sh`](check-portable-sed.sh) | CI guard that blocks non-portable in-place `sed` usage (see the root README's Shell Script Portability guideline). |
| [`servicenow-cleanup.py`](servicenow-cleanup.py) | List and delete ServiceNow incidents (e.g. test/demo tickets) with filters; dry-run by default. |

This section documents repo-wide standalone administration and maintenance utilities located under `scripts/`.

## ServiceNow Incident Cleanup Utility (`scripts/servicenow-cleanup.py`)

A cross-prototype standalone administration tool to inspect and safely purge incident records from ServiceNow.

> [!WARNING]
> **Shared Environment Safety Warning:**
> ServiceNow instances are often shared across teams and agent prototypes. When filtering by age alone (e.g. `--older-than N`), this tool matches tickets from **all sources** created prior to that cutoff. Always inspect the dry-run output carefully or supply narrowing filters (such as `--created-by`, `--number-from`/`--number-to`, `--caller`, or `--state`) to avoid deleting tickets belonging to other prototypes or human engineers.

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

### Security Architecture

- **No Secrets in Logs or History:** Passwords are never logged, echoed, or persisted. Log output automatically sanitizes authorization tokens and secret strings.
- **No `--password` CLI Flag:** The script deliberately omits a `--password` flag to prevent credentials from landing in shell history files or process tables (`ps aux`). Passwords must be provided via the `SNOW_PASSWORD` environment variable or entered securely when prompted interactively via `getpass`.
- **Standalone Lightweight Dependencies:** Built strictly using Python standard library and `requests`. Does not require Google Cloud SDK or Secret Manager dependencies.
- **Mocks Only for Agent Testing:** Automated agent suites test this utility exclusively with mocks and recorded fixtures; real credentials or live instances are never touched by development agents.

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
