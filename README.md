# autocloud-prototypes
A repo of Autocloud prototypes

## Prototypes

### [Agent Registry Demo](agent-registry-demo/) (@ssvaidyanathan)

| Specification | Details |
| :--- | :--- |
| **Core Architecture** | Dynamic skill and tool loading from GCP Agent Registry with zero-downtime hot-reloading |
| **Runtime & Protocols** | Custom FastAPI exposing `POST /chat` and `POST /admin/cache/invalidate`; FastMCP over Streamable HTTP |
| **Integrations** | GCP Agent Registry (`v1alpha` skills, `v1` tools), Vertex AI Gemini |
| **Cloud Run Services (default prefix)** | `ard-agent-service`, `ard-calculator-service` |
| **State & Persistence** | Pure in-memory ZIP unpacking (zero local disk persistence for skills); TTL in-memory cache with explicit invalidation |
| **Docs & Quick-Start** | [Documentation](agent-registry-demo/) • [Hot-Reload Demo](agent-registry-demo/#8-hot-reload-demonstration-scenarios) |

### [Ambient Agent Registry Demo](ambient-agent-registry-demo/) (@ssvaidyanathan)

| Specification | Details |
| :--- | :--- |
| **Core Architecture** | Native ADK runtime with dynamic skill/tool loading from GCP Agent Registry and zero-downtime hot-reloading |
| **Runtime & Protocols** | Native Google ADK `LlmAgent` + `Runner` exposing synchronous `POST /run` (`list[Event]`), `POST /run_sse`, and `POST /admin/cache/invalidate`; FastMCP over Streamable HTTP |
| **Integrations** | GCP Agent Registry (`v1alpha` skills, `v1` tools), Vertex AI Gemini (2.5 Flash) |
| **Cloud Run Services (default prefix)** | `ambient-agent-service`, `ambient-calculator-service` |
| **State & Persistence** | Pure in-memory ZIP unpacking (zero local disk persistence for skills); in-memory TTL cache with explicit atomic invalidation |
| **Docs & Quick-Start** | [Documentation](ambient-agent-registry-demo/) • [Hot-Reload Demo](ambient-agent-registry-demo/#8-live-demonstration-execution) |

### [Incident SRE Agent](incident-sre-agent/) (@ssvaidyanathan)

| Specification | Details |
| :--- | :--- |
| **Core Architecture** | Autonomous GCP incident investigation across logs, metrics, changes, and assets with ITSM sync |
| **Runtime & Protocols** | Asynchronous webhook ingestion bridge (`POST /webhook/{servicenow,jira,generic}`) + Cloud Tasks worker |
| **Integrations** | ServiceNow (Flow Designer), Jira Cloud, Cloud Logging/Monitoring/Audit/Asset, Vertex AI Gemini |
| **Cloud Run Services (default prefix)** | `isa-incident-bridge`, `isa-sre-agent` |
| **State & Persistence** | Stateless Cloud Run services; Cloud Tasks asynchronous queue; standalone HTML dossier generation |
| **Docs & Quick-Start** | [Documentation](incident-sre-agent/) • [Deployment Script](incident-sre-agent/#10-master-automated-deployment-script) |

### [GitHub Actions Deployment Failure Investigator](gha-deploy-investigator/) (@ssvaidyanathan)

| Specification | Details |
| :--- | :--- |
| **Core Architecture** | Autonomous CI/CD pipeline failure triage, Gemini log correlation, and bidirectional write-back |
| **Runtime & Protocols** | Ingestion bridge receiving GitHub App webhooks (`workflow_run`, `workflow_job`) + Cloud Tasks worker |
| **Integrations** | GitHub App (Check Runs, PRs, Actions API), ServiceNow (Table & Attachment API), Cloud Run, Vertex AI |
| **Cloud Run Services (default prefix)** | `gdi-bridge`, `gdi-agent` |
| **State & Persistence** | Stateless event processing; Cloud Tasks asynchronous queue; in-memory query cache |
| **Docs & Quick-Start** | [Documentation](gha-deploy-investigator/) • [Deployment Walkthrough](gha-deploy-investigator/#9-full-step-by-step-gcp-deployment-walkthrough) |

## Shared Utilities

This section documents repo-wide standalone administration and maintenance utilities located under `scripts/`.

### ServiceNow Incident Cleanup Utility (`scripts/servicenow-cleanup.py`)

A cross-prototype standalone administration tool to inspect and safely purge incident records from ServiceNow.

> [!WARNING]
> **Shared Environment Safety Warning:**
> ServiceNow instances are often shared across teams and agent prototypes. When filtering by age alone (e.g. `--older-than N`), this tool matches tickets from **all sources** created prior to that cutoff. Always inspect the dry-run output carefully or supply narrowing filters (such as `--created-by`, `--number-from`/`--number-to`, `--caller`, or `--state`) to avoid deleting tickets belonging to other prototypes or human engineers.

#### Operational Model & Defaults

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

#### Security Architecture

- **No Secrets in Logs or History:** Passwords are never logged, echoed, or persisted. Log output automatically sanitizes authorization tokens and secret strings.
- **No `--password` CLI Flag:** The script deliberately omits a `--password` flag to prevent credentials from landing in shell history files or process tables (`ps aux`). Passwords must be provided via the `SNOW_PASSWORD` environment variable or entered securely when prompted interactively via `getpass`.
- **Standalone Lightweight Dependencies:** Built strictly using Python standard library and `requests`. Does not require Google Cloud SDK or Secret Manager dependencies.
- **Mocks Only for Agent Testing:** Automated agent suites test this utility exclusively with mocks and recorded fixtures; real credentials or live instances are never touched by development agents.

#### CLI Parameters

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

#### Usage Examples

```bash
## 1. Default dry-run: audit tickets older than 7 days
python scripts/servicenow-cleanup.py \
  --instance-url https://dev12345.service-now.com \
  --username admin

## 2. Dry-run export to CSV for resolved and closed tickets older than 14 days
python scripts/servicenow-cleanup.py \
  --older-than 14 \
  --state resolved,closed \
  --output csv > stale_incidents.csv

## 3. Interactive apply: prompts to enter exact incident count before deleting
python scripts/servicenow-cleanup.py \
  --created-by svc_integration \
  --created-before 2026-10-15 \
  --apply

## 4. Non-interactive apply with --yes for explicit ticket cleanup
export SNOW_INSTANCE_URL="https://dev12345.service-now.com"
export SNOW_USERNAME="admin"
export SNOW_PASSWORD="<credentials-from-secure-store>"

python scripts/servicenow-cleanup.py \
  --numbers INC0010222,INC0010223,INC0010224 \
  --apply \
  --yes
```

## Development Guidelines

### Shell Script Portability
All shell scripts and CI workflow `run:` blocks must remain portable across macOS (BSD) and Linux (GNU). Any in-place `sed` editing must use the uname-based `sedi_args` array pattern (`sedi_args=("-i")`; on Darwin `sedi_args=("-i" "")`). Non-portable variants (bare `sed -i`, `sed -i ''`, `sed -i.bak`, or `sed --in-place`) are blocked by `scripts/check-portable-sed.sh`.

Note: Heredoc bodies (e.g. `cat << 'EOF'`) are scanned as shell code. If a script generates documentation or user instructions referencing `sed`, reword the text (e.g., describe in-place editing conceptually or avoid literal `-i` flags) to satisfy the guard.
