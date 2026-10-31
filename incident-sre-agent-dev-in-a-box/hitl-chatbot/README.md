# Human-In-The-Loop (HITL) SRE Chatbot

SRE assistant built with Google Agent Development Kit (ADK 2.11) and FastAPI.

Capabilities:
- Correlates telemetry and deployment context.
- Drafts incident tickets.
- Enforces Human-In-The-Loop (HITL) approval gates before mutating actions.

> **Slack Integration:**
> Mounted at `/slack/events` via `app/adapters/slack.py`. See [docs/slack_integration.md](docs/slack_integration.md).

---

## 1. System Architecture

```text
               +------------------------------------------+
               |        Engineer Interaction Channels     |
               |     (CLI Runner / REST API Client)       |
               +--------------------+---------------------+
                                    |
                                    v
                       +-------------------------+
                       | FastAPI REST Layer      |
                       | (/api/*, Google OIDC)   |
                       +------------+------------+
                                    |
                                    v
                     +-----------------------------+
                     | ChatbotAgentRunner          |
                     | - Intent Routing            |
                     | - ConversationStore         |
                     +--------------+--------------+
                                    |
               +--------------------+--------------------+
               |                                         |
               v                                         v
   +-----------------------+                 +-----------------------+
   | Context & Diagnostics |                 | Mutating HITL Gate    |
   | - MCP Client          |                 | - HMAC-SHA256 Tokens  |
   | - Cloud Monitoring    |                 | - Requester-Only Auth |
   +-----------------------+                 | - Jira Ticket Create  |
                                             +-----------------------+
```

### Components
- **Slack Bolt Adapter (`app/adapters/slack.py`):** Handles Slack Events API and interactivity at `/slack/events`. Acknowledges in <3s, renders Block Kit cards, enforces requester-only gates, and posts RCA reports to threads.
- **Agent Core (`app/agent.py`):** Coordinates tool calls, intent routing, and turn execution using `app/prompts/chatbot.yaml`.
- **Session Store (`app/session.py`):** In-memory store for message history, ticket drafts, active ticket context, and cached RCA reports with TTL purge. Uses composite thread keys (`channel:thread_ts`).
- **Approval Tokens (`app/tokens.py`):** Issues time-limited HMAC-SHA256 signed approval tokens bound to `requester_id`. Rejects unauthorized approvers and replay attempts.
- **REST API (`app/main.py`):** Authenticated endpoints (`/api/chat`, `/api/chat/approve`, `/api/investigations/{ticket_key}`) and bridge callback (`/internal/rca-callback`).
- **CLI Adapter (`app/adapters/cli.py`, `scripts/chat-cli.py`):** Terminal interface for testing and demo scenarios.

---

## 2. Key Use Cases

| Use Case | Description | Tools / Endpoints |
| --- | --- | --- |
| **A: Rollout Context Query** | Queries recent deployments, scaling events, and config changes. | `mcp-deploy-context-stub` (`lookup_alert_context`, `get_recent_deployments`) |
| **B: Triage & Jira Draft Gate** | Resolves alert policies, extracts violation onset (`alert_started_at`), drafts a Jira ticket, and holds `create_jira_issue` behind human approval. | `app/tools/alert.py`, `app/tools/jira_draft.py`, `app/tools/jira_create.py` |

---

## 3. HITL Approval Model

Mutating operations require approval via signed tokens:

| Property | Implementation |
| --- | --- |
| **Identity Binding** | Token embeds `requester_id`. Other users receive HTTP 403. |
| **Signature** | HMAC-SHA256 over base64url payload verified against `APPROVAL_SECRET_KEY`. |
| **Replay Protection** | Approved tokens are registered in an in-memory `TokenReplayRegistry`. Second use fails. |
| **TTL** | Tokens expire after `TOKEN_TTL_SECONDS` (default: 300s). |

---

## 4. Cloud Monitoring Lookup & Violation Propagation

- **Policy Resolution (`app/tools/alert.py`):** Queries Cloud Monitoring APIs (`v3/projects/{project}/alertPolicies`, `v3/projects/{project}/incidents`). Matches policy display names by substring.
- **Violation Onset (`alert_started_at`):** Extracts condition breach timestamp and forwards it to draft and Jira metadata for downstream lookback windows.

---

## 5. Slack Integration

Mounted at `/slack/events` using Slack Bolt (`app/adapters/slack.py`).

| Feature | Implementation |
| --- | --- |
| **Events** | Subscribes to `app_mention` and `message.im`. Verifies requests with HMAC-SHA256 (`X-Slack-Signature`). |
| **Response Time** | Calls `await ack()` in <3s, then processes turns via `asyncio.create_task`. |
| **UI** | Block Kit cards with `[Approve & Create Ticket]`, `[Approve & Add Comment]`, `[Cancel]`. |
| **Gate** | Rejects non-requester clicks with ephemeral error. Token remains valid for requester. |
| **Threads** | Tracks state by `channel:thread_ts`. In-thread updates route to comments, preventing duplicates. |
| **RCA Delivery** | Bridge calls `/internal/rca-callback`; adapter posts summary, root cause, and dossier link to thread. |
| **Setup Guide** | See [docs/slack_integration.md](docs/slack_integration.md). |

---

## 6. Configuration & Environment Variables

| Variable | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `APP_ENV` | string | `local` | Application environment (`local`, `dev`, `prod`) |
| `PROJECT_ID` | string | `""` | Target Google Cloud project ID |
| `PORT` | integer | `8080` | Bind port for FastAPI application |
| `APPROVAL_SECRET_KEY` | string | (random) | Secret key for signing HMAC approval tokens |
| `TOKEN_TTL_SECONDS` | integer | `300` | Expiration lifetime for approval tokens |
| `BRIDGE_BASE_URL` | string | `""` | URL of the incident bridge service (`dib-incident-bridge`) |
| `MCP_SERVER_URL` | string | `""` | Streamable HTTP endpoint of the deployment context stub |
| `SERVICE_URL` | string | `""` | Public or internal URL of the chatbot service |
| `SLACK_BOT_TOKEN` | string | `""` | Bot User OAuth Token (`xoxb-...`) for posting Slack messages and Block Kit cards |
| `SLACK_SIGNING_SECRET` | string | `""` | Slack application signing secret for validating inbound `/slack/events` webhooks |
| `AUTH_DISABLED` | boolean | `false` | Disable OIDC token verification for local testing |
| `AUTHORIZED_CALLER_SERVICE_ACCOUNTS` | string | `""` | Comma-separated service accounts authorized to invoke `/api/*` |
| `BRIDGE_CALLER_SERVICE_ACCOUNTS` | string | `""` | Service accounts authorized to invoke `/internal/rca-callback` |
| `EXPECTED_AUDIENCE` | string | `""` | Optional expected audience for OIDC verification |

---

## 7. Directory Structure

```text
hitl-chatbot/
├── README.md                          # Service documentation & runbook
├── pyproject.toml                     # Package dependencies and configuration
├── Dockerfile                         # Production multi-stage container build
├── docs/
│   └── slack_integration.md           # End-to-end Slack App setup & operations guide
├── app/
│   ├── __init__.py
│   ├── main.py                        # FastAPI entrypoint & route handlers
│   ├── config.py                      # Pydantic Settings configuration
│   ├── auth.py                        # Google Cloud OIDC authentication middleware
│   ├── agent.py                       # Chatbot agent runner & turn processing
│   ├── session.py                     # In-memory ConversationStore (composite thread keys)
│   ├── tokens.py                      # HMAC-SHA256 token verification & replay registry
│   ├── prompts/
│   │   └── chatbot.yaml               # Externalized system prompts and schemas
│   ├── tools/
│   │   ├── __init__.py
│   │   ├── alert.py                   # Cloud Monitoring alert & incident lookup
│   │   ├── mcp_client.py              # Deployment context MCP client
│   │   ├── jira_draft.py              # Ticket draft generator
│   │   └── jira_create.py             # Jira issue creation tool (mutating gate)
│   └── adapters/
│       ├── __init__.py
│       ├── cli.py                     # Terminal CLI adapter
│       └── slack.py                   # Slack Bolt asynchronous event adapter
├── scripts/
│   └── chat-cli.py                    # Interactive CLI runner
└── tests/
    ├── __init__.py
    ├── conftest.py                    # Pytest fixtures and mock servers
    ├── test_tokens.py                 # HMAC token security & replay tests
    ├── test_session.py                # Conversation history & sliding window tests
    ├── test_tools.py                  # Alert, MCP, and Jira tool tests
    ├── test_auth.py                   # OIDC authentication middleware tests
    ├── test_agent_cli.py              # Agent turn and CLI runner tests
    └── test_slack.py                  # Slack Bolt adapter and HITL gate tests
```

---

## 8. Local Development & Testing

### 8.1. Setup Environment
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### 8.2. Run Unit & Conformance Tests
```bash
pytest -v tests/
```

### 8.3. Run Interactive CLI Runner
```bash
# Run Use Case A (Rollout query)
python scripts/chat-cli.py --case A

# Run Use Case B (Interactive Jira ticket creation with confirmation)
python scripts/chat-cli.py --case B

# Run Use Case B with auto-confirmation flag
python scripts/chat-cli.py --case B --auto-confirm
```

### 8.4. Run Local FastAPI Server
```bash
export AUTH_DISABLED=true
export PORT=8080
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```
Verify health:
```bash
curl -s http://localhost:8080/health
```

---

## 9. Deployment (Google Cloud Run)

### Container Build
```bash
docker build -t us-central1-docker.pkg.dev/${PROJECT_ID}/hitl-chatbot/hitl-chatbot:latest .
docker push us-central1-docker.pkg.dev/${PROJECT_ID}/hitl-chatbot/hitl-chatbot:latest
```

### Cloud Run Deploy
```bash
gcloud run deploy hitl-chatbot \
  --image="us-central1-docker.pkg.dev/${PROJECT_ID}/hitl-chatbot/hitl-chatbot:latest" \
  --region="us-central1" \
  --platform="managed" \
  --no-allow-unauthenticated \
  --ingress="internal-and-cloud-load-balancing" \
  --min-instances=1 \
  --set-env-vars="PROJECT_ID=${PROJECT_ID},BRIDGE_BASE_URL=https://dib-incident-bridge-url"
```
