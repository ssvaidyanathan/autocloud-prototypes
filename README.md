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
| **Docs & Quick-Start** | [Documentation](agent-registry-demo/) • [Deployment](agent-registry-demo/#7-infrastructure-deployment-walkthrough) • [Hot-Reload Demo](agent-registry-demo/#8-hot-reload-demonstration-scenarios) |

### [Ambient Agent Registry Demo](ambient-agent-registry-demo/) (@ssvaidyanathan)

| Specification | Details |
| :--- | :--- |
| **Core Architecture** | Native ADK runtime with dynamic skill/tool loading from GCP Agent Registry and zero-downtime hot-reloading |
| **Runtime & Protocols** | Native Google ADK `LlmAgent` + `Runner` exposing synchronous `POST /run` (`list[Event]`), `POST /run_sse`, and `POST /admin/cache/invalidate`; FastMCP over Streamable HTTP |
| **Integrations** | GCP Agent Registry (`v1alpha` skills, `v1` tools), Vertex AI Gemini (2.5 Flash) |
| **Cloud Run Services (default prefix)** | `ambient-agent-service`, `ambient-calculator-service` |
| **State & Persistence** | Pure in-memory ZIP unpacking (zero local disk persistence for skills); in-memory TTL cache with explicit atomic invalidation |
| **Docs & Quick-Start** | [Documentation](ambient-agent-registry-demo/) • [Deployment](ambient-agent-registry-demo/#7-gcp-infrastructure-deployment-terraform) • [Hot-Reload Demo](ambient-agent-registry-demo/#8-live-demonstration-execution) |

### [Incident SRE Agent](incident-sre-agent/) (@ssvaidyanathan)

| Specification | Details |
| :--- | :--- |
| **Core Architecture** | Autonomous GCP incident investigation across logs, metrics, changes, and assets with ITSM sync |
| **Runtime & Protocols** | Asynchronous webhook ingestion bridge (`POST /webhook/{servicenow,jira,generic}`) + Cloud Tasks worker |
| **Integrations** | ServiceNow (Flow Designer), Jira Cloud, Cloud Logging/Monitoring/Audit/Asset, Vertex AI Gemini |
| **Cloud Run Services (default prefix)** | `isa-incident-bridge`, `isa-sre-agent` |
| **State & Persistence** | Stateless Cloud Run services; Cloud Tasks asynchronous queue; standalone HTML dossier generation |
| **Docs & Quick-Start** | [Documentation](incident-sre-agent/) • [Deployment](incident-sre-agent/docs/DEPLOY.md) • [Demo Scenarios](incident-sre-agent/demo/SCENARIOS.md) • [Operations](incident-sre-agent/docs/OPERATIONS.md) • [Architecture](incident-sre-agent/docs/ARCHITECTURE.md) • [GKE Investigation](incident-sre-agent/docs/GKE_INVESTIGATION.md) |

### [Incident SRE Agent (Dev In A Box)](incident-sre-agent-dev-in-a-box/) (@sermolin)

A dedicated development sandbox fork of incident-sre-agent for Human-In-The-Loop (HITL) and chat-driven incident response workflows.

| Specification | Details |
| :--- | :--- |
| **Core Architecture** | Autonomous GCP incident investigation sandbox with FastMCP streamable HTTP deployment context server (`mcp-deploy-context-stub`), bridge HITL incident endpoints (`POST /api/jira/issue`, issue status, `POST /api/jira/issue/{key}/comment`) with Jira ADF conversion and automatic OIDC RCA callback forward, and standalone HITL SRE Chatbot engine (`hitl-chatbot`: runner, CLI, OIDC REST API, Cloud Monitoring lookup, signed requester-only approvals, active ticket thread context, duplicate guard). (In progress: Slack integration — see PR 5). |
| **Runtime & Protocols** | Asynchronous webhook bridge (`POST /webhook/{servicenow,jira,generic}`) + Cloud Tasks worker |
| **Integrations** | ServiceNow, Jira Cloud, Cloud Logging/Monitoring/Audit/Asset, Vertex AI Gemini, FastMCP streamable HTTP server (`mcp-deploy-context-stub`), bridge HITL incident endpoints (`POST /api/jira/issue`, issue status, `POST /api/jira/issue/{key}/comment`) with Jira ADF conversion and automatic OIDC RCA callback forward, and standalone HITL SRE Chatbot engine (`hitl-chatbot`: runner, CLI, OIDC REST API, Cloud Monitoring lookup, signed requester-only approvals, active ticket thread context, duplicate guard) |
| **Cloud Run Services (default prefix)** | `<prefix>-incident-bridge`, `<prefix>-sre-agent` (upcoming: `hitl-chatbot`, `mcp-deploy-context-stub`) |
| **State & Persistence** | Stateless Cloud Run services; Cloud Tasks asynchronous queue; standalone HTML dossier generation |
| **Docs & Quick-Start** | [Documentation](incident-sre-agent-dev-in-a-box/) • [Deployment](incident-sre-agent-dev-in-a-box/docs/DEPLOY.md) • [Demo Scenarios](incident-sre-agent-dev-in-a-box/demo/SCENARIOS.md) • [Operations](incident-sre-agent-dev-in-a-box/docs/OPERATIONS.md) • [Architecture](incident-sre-agent-dev-in-a-box/docs/ARCHITECTURE.md) • [GKE Investigation](incident-sre-agent-dev-in-a-box/docs/GKE_INVESTIGATION.md) • [MCP Deploy Stub](incident-sre-agent-dev-in-a-box/mcp-deploy-context-stub/README.md) • [Contract](incident-sre-agent-dev-in-a-box/mcp-deploy-context-stub/CONTRACT.md) • [HITL Endpoints](incident-sre-agent-dev-in-a-box/bridge/HITL_ENDPOINTS.md) • [HITL Chatbot](incident-sre-agent-dev-in-a-box/hitl-chatbot/README.md) |


### [GitHub Actions Deployment Failure Investigator](gha-deploy-investigator/) (@ssvaidyanathan)

| Specification | Details |
| :--- | :--- |
| **Core Architecture** | Autonomous CI/CD pipeline failure triage, Gemini log correlation, and bidirectional write-back |
| **Runtime & Protocols** | Ingestion bridge receiving GitHub App webhooks (`workflow_run`, `workflow_job`) + Cloud Tasks worker |
| **Integrations** | GitHub App (Check Runs, PRs, Actions API), ServiceNow (Table & Attachment API), Cloud Run, Vertex AI |
| **Cloud Run Services (default prefix)** | `gdi-bridge`, `gdi-agent` |
| **State & Persistence** | Stateless event processing; Cloud Tasks asynchronous queue; in-memory query cache |
| **Docs & Quick-Start** | [Documentation](gha-deploy-investigator/) • [Deployment](gha-deploy-investigator/docs/DEPLOY.md) • [Architecture](gha-deploy-investigator/docs/ARCHITECTURE.md) |

### [Alert Triage Agent](alert-triage-agent/) (@ssvaidyanathan)

| Specification | Details |
| :--- | :--- |
| **Core Architecture** | Cloud Monitoring alert triage: agent-owned grouping (topology, causal chain, time, shared deploy; Gemini for gray-zone cases), suppression, evidence-based origin/root cause, one ServiceNow incident per group |
| **Runtime & Protocols** | Lean bridge (Basic-auth webhook + ServiceNow instruction endpoints) + Cloud Tasks (triage and delayed settle/cool-down timers) + ADK agent on Cloud Run |
| **Integrations** | Cloud Monitoring, Cloud Run, Apigee X, Cloud SQL, Cloud Logging, ServiceNow (Table & Attachment API), Vertex AI |
| **Cloud Run Services (default prefix)** | `ata-bridge`, `ata-agent`, `ata-tools` |
| **State & Persistence** | Incident group state as markdown (OKF) files on GCS with generation-match preconditions |
| **Docs & Quick-Start** | [Documentation](alert-triage-agent/) • [Deployment](alert-triage-agent/DEPLOY.md) • [Demo Scenarios](alert-triage-agent/DEMO.md) |

## Shared Utilities

Repo-wide scripts live in [`scripts/`](scripts/). See [`scripts/README.md`](scripts/README.md) for the list and usage of each.

## Development Guidelines

### Shell Script Portability
All shell scripts and CI workflow `run:` blocks must remain portable across macOS (BSD) and Linux (GNU). Any in-place `sed` editing must use the uname-based `sedi_args` array pattern (`sedi_args=("-i")`; on Darwin `sedi_args=("-i" "")`). Non-portable variants (bare `sed -i`, `sed -i ''`, `sed -i.bak`, or `sed --in-place`) are blocked by `scripts/check-portable-sed.sh`.

Note: Heredoc bodies (e.g. `cat << 'EOF'`) are scanned as shell code. If a script generates documentation or user instructions referencing `sed`, reword the text (e.g., describe in-place editing conceptually or avoid literal `-i` flags) to satisfy the guard.
