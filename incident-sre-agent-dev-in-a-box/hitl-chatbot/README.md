# Human-In-The-Loop (HITL) SRE Chatbot Prototype

An interactive, autonomous Site Reliability Engineering (SRE) assistant built with Google Agent Development Kit (ADK 2.11) and FastAPI.

The chatbot provides engineers with fast telemetry correlation, deployment context lookup, automated ticket drafting, and cryptographically verified Human-In-The-Loop (HITL) mutating approval gates.

> **Slack Integration (Enabled):**
> Slack integration is enabled via the Slack Bolt adapter (`app/adapters/slack.py`) and `/slack/events` routing. For the complete Slack app manifest, setup instructions, secrets reference, and troubleshooting guide, see [docs/slack_integration.md](docs/slack_integration.md).

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
- **Slack Bolt Adapter (`app/adapters/slack.py`):** Asynchronous Slack Events API and Interactivity router (`/slack/events`). Enforces `<3s` event acknowledgment, renders interactive Block Kit approval cards, enforces requester-only gates, and posts completed RCA reports into incident threads.
- **Agent Core (`app/agent.py`):** Drives conversational turns using prompt definitions in `app/prompts/chatbot.yaml`. Coordinates specialist tool calls, intent routing, and conversation lifecycle.
- **Session & In-Memory Store (`app/session.py`):** Maintains sliding message history, pending ticket drafts, active ticket context, and cached Root Cause Analysis (RCA) reports with TTL-based purge. Supports composite thread keying (`channel:thread_ts`). Zero external database required.
- **Cryptographic Gate (`app/tokens.py`):** Issues time-limited HMAC-SHA256 signed approval tokens cryptographically bound to the initiating `requester_id`. Prevents token tampering, unauthorized approvers, and replay attacks.
- **REST API (`app/main.py`):** Exposes authenticated endpoints (`/api/chat`, `/api/chat/approve`, `/api/investigations/{ticket_key}`) and the internal bridge callback (`/internal/rca-callback`).
- **CLI Adapter (`app/adapters/cli.py`, `scripts/chat-cli.py`):** Interactive terminal interface supporting automated demo scenarios and interactive confirmation workflows.

---

## 2. Key Use Cases

### Use Case A: Deployment Rollout Context Query
Directly queries recent deployment events from the Deployment Context FastMCP stub server (`lookup_alert_context` and `get_recent_deployments`). Correlates service health alerts with recent container rollouts, scaling events, and configuration changes without navigating multiple dashboards.

### Use Case B: Incident Triage & Jira Draft Gate
1. Resolves fuzzy alert policy names against Google Cloud Monitoring.
2. Extracts violation onset (`alert_started_at`) and incident metrics.
3. Generates a structured Jira ticket draft with formatted Detection, Impact, Trigger, and Mitigation sections.
4. Generates an HMAC-SHA256 signed approval token bound to the requesting user.
5. Mutating side-effect (`create_jira_issue`) is blocked until explicit human approval is received.

---

## 3. HITL Security & Approval Model

The chatbot enforces a strict zero-trust approval gate for all mutating infrastructure operations:
- **Requester-Only Identity Binding:** When a user initiates a draft, their user identity (`requester_id`) is embedded in the signed payload. Only the identical user can execute the approval. Attempts by other users to approve the token are rejected with HTTP 403 Forbidden.
- **HMAC-SHA256 Signature:** Tokens contain base64url-encoded JSON payloads verified against `APPROVAL_SECRET_KEY`. Any payload tampering causes signature mismatch.
- **Single-Use Replay Protection:** Executed tokens are recorded in an in-memory `TokenReplayRegistry`. Repeated submission of an approved token fails immediately.
- **TTL Eviction:** Tokens expire after 300 seconds (configurable via `TOKEN_TTL_SECONDS`).

---

## 4. Cloud Monitoring Lookup & Violation Propagation

- **Dynamic Policy Resolution (`app/tools/alert.py`):** Queries Cloud Monitoring REST APIs (`v3/projects/{project}/alertPolicies` and `v3/projects/{project}/incidents`) using Google Cloud credentials. Supports fuzzy substring matching across policy display names.
- **Violation Start Time (`alert_started_at`):** Captures the exact timestamp when the condition was breached. Propagates this timestamp into the draft ticket and Jira issue metadata, ensuring downstream investigation agents anchor their diagnostic lookback window correctly.

---

## 5. Slack Integration (Bolt Adapter & Block Kit Cards)

The chatbot includes a native Slack Bolt asynchronous adapter (`app/adapters/slack.py`) mounted at `/slack/events`:

- **Event Subscriptions & Webhook Verification:** Subscribes to channel mentions (`app_mention`) and direct messages (`message.im`). Inbound requests are cryptographically verified using HMAC-SHA256 (`X-Slack-Signature`) against the workspace's signing secret (`hitl-slack-signing-secret`).
- **Strict Acknowledgment (<3s) & Async Worker:** Adheres strictly to Slack's 3-second acknowledgment window. Event handlers acknowledge receipt immediately (`await ack()`) and dispatch turn processing to an asynchronous background worker (`asyncio.create_task`).
- **Interactive Block Kit Cards:** Renders formatted message cards for ticket previews with interactive buttons (`[Approve & Create Ticket]`, `[Approve & Add Comment]`, and `[Cancel]`).
- **Cryptographic Requester-Only Authorization Gate:** Signed approval tokens cryptographically bind the initiating user's Slack ID (`requester_id`). If an unauthorized user clicks `[Approve]`, the adapter responds with an ephemeral rejection (`Approval rejected: user '...' is not authorized to approve draft created by '...'`), leaving the single-use token intact for the authorized requester.
- **Thread Context & Active Ticket Tracking:** Conversations maintain thread-scoped state in `ConversationStore` using composite keys (`channel:thread_ts`). In-thread follow-up prompts ("update the ticket with your RCA") are detected by the intent classifier and routed directly to comment flows without creating duplicate tickets.
- **Automated Root-Cause Analysis (RCA) Post-Back:** When the `dib-sre-agent` completes an investigation, the bridge posts an asynchronous callback to `/internal/rca-callback`. The Slack adapter automatically resolves the originating thread reference and posts the executive summary, root cause, and diagnostic report link back to the incident thread.
- **Setup & Operations Runbook:** For the complete Slack app manifest, bot scopes, step-by-step installation instructions, Secret Manager commands, and troubleshooting runbooks, see [docs/slack_integration.md](docs/slack_integration.md).

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
