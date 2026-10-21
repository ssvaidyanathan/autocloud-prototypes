# autocloud-prototypes
A repo of Autocloud prototypes

## Prototypes

### [Agent Registry Demo](agent-registry-demo/)

| Specification | Details |
| :--- | :--- |
| **Core Architecture** | Dynamic skill and tool loading from GCP Agent Registry with zero-downtime hot-reloading |
| **Runtime & Protocols** | Custom FastAPI exposing `POST /chat` and `POST /admin/cache/invalidate`; FastMCP over Streamable HTTP |
| **Integrations** | GCP Agent Registry (`v1alpha` skills, `v1` tools), Vertex AI Gemini |
| **Cloud Run Services (default prefix)** | `ard-agent-service`, `ard-calculator-service` |
| **State & Persistence** | Pure in-memory ZIP unpacking (zero local disk persistence for skills); TTL in-memory cache with explicit invalidation |
| **Docs & Quick-Start** | [Documentation](agent-registry-demo/) • [Hot-Reload Demo](agent-registry-demo/#8-hot-reload-demonstration-scenarios) |

### [Incident SRE Agent](incident-sre-agent/)

| Specification | Details |
| :--- | :--- |
| **Core Architecture** | Autonomous GCP incident investigation across logs, metrics, changes, and assets with ITSM sync |
| **Runtime & Protocols** | Asynchronous webhook ingestion bridge (`POST /webhook/{servicenow,jira,generic}`) + Cloud Tasks worker |
| **Integrations** | ServiceNow (Flow Designer), Jira Cloud, Cloud Logging/Monitoring/Audit/Asset, Vertex AI Gemini |
| **Cloud Run Services (default prefix)** | `isa-incident-bridge`, `isa-sre-agent` |
| **State & Persistence** | Stateless Cloud Run services; Cloud Tasks asynchronous queue; standalone HTML dossier generation |
| **Docs & Quick-Start** | [Documentation](incident-sre-agent/) • [Deployment Script](incident-sre-agent/#10-master-automated-deployment-script) |

### [GitHub Actions Deployment Failure Investigator](gha-deploy-investigator/)

| Specification | Details |
| :--- | :--- |
| **Core Architecture** | Autonomous CI/CD pipeline failure triage, Gemini log correlation, and bidirectional write-back |
| **Runtime & Protocols** | Ingestion bridge receiving GitHub App webhooks (`workflow_run`, `workflow_job`) + Cloud Tasks worker |
| **Integrations** | GitHub App (Check Runs, PRs, Actions API), ServiceNow (Table & Attachment API), Cloud Run, Vertex AI |
| **Cloud Run Services (default prefix)** | `gdi-bridge`, `gdi-agent` |
| **State & Persistence** | Stateless event processing; Cloud Tasks asynchronous queue; in-memory query cache |
| **Docs & Quick-Start** | [Documentation](gha-deploy-investigator/) • [Deployment Walkthrough](gha-deploy-investigator/#9-full-step-by-step-gcp-deployment-walkthrough) |

## Development Guidelines

### Shell Script Portability
All shell scripts and CI workflow `run:` blocks must remain portable across macOS (BSD) and Linux (GNU). Any in-place `sed` editing must use the uname-based `sedi_args` array pattern (`sedi_args=("-i")`; on Darwin `sedi_args=("-i" "")`). Non-portable variants (bare `sed -i`, `sed -i ''`, `sed -i.bak`, or `sed --in-place`) are blocked by `scripts/check-portable-sed.sh`.

Note: Heredoc bodies (e.g. `cat << 'EOF'`) are scanned as shell code. If a script generates documentation or user instructions referencing `sed`, reword the text (e.g., describe in-place editing conceptually or avoid literal `-i` flags) to satisfy the guard.

