# Dev-In-A-Box HITL Incident Response Architecture

## 1. Scope

This document specifies the Dev-In-A-Box (`incident-sre-agent-dev-in-a-box`) Human-In-The-Loop (HITL) incident response architecture across conversational Slack interactions, deployment context retrieval, incident ticket lifecycle, asynchronous queueing, and automated root cause analysis (RCA). Components include the Slack conversational bot (`hitl-chatbot`), the deployment context server (`mcp-deploy-context-stub`), the incident bridge (`dib-incident-bridge`), Jira integration and automation rules, Cloud Tasks asynchronous queue, and the multi-specialist SRE investigation agent (`dib-sre-agent`).

## 2. Full System Topology

```mermaid
flowchart TD
    Slack["Slack Workspace"]
    subgraph CloudRun["Google Cloud Run"]
        subgraph Chatbot["hitl-chatbot"]
            Handler["Bolt AsyncSlackRequestHandler\n(verifies X-Slack-Signature; acks <3s)"]
            Router["Event / Action Router\n(app_mention, message, block_actions)"]
            Core["Core Runner & Approval Gate\n(agent.py, tokens.py, session.py)"]
            Client["Bolt AsyncWebClient\n(slack_sdk, via Bolt)"]
            Handler --> Router --> Core --> Client
        end
    end
    CM["Cloud Monitoring"]
    MCP["mcp-deploy-context-stub\n(/mcp, tools/call)"]
    Bridge["dib-incident-bridge\n(/api/jira/issue, /api/jira/issue/{key}/comment,\nstatus, /webhook/jira, /callback/report)"]
    Jira["Jira & Jira Automation\n(trigger: label hitl)"]
    CT["Cloud Tasks"]
    subgraph Agent["dib-sre-agent (/investigate)"]
        Triage["Triage -> Specialists -> Synthesizer"]
        GKE["GKE Tools"]
        Triage --> GKE
    end
    SM["Secret Manager"]

    Slack -->|"POST /slack/events (X-Slack-Signature)"| Handler
    Client -->|"HTTPS REST (xoxb bot token)"| Slack
    SM -.->|"hitl-slack-signing-secret, hitl-slack-bot-token"| Chatbot
    SM -.->|"Jira API token, X-Webhook-Token"| Bridge

    Core -->|"HTTPS REST (Google Cloud IAM / ADC)"| CM
    Core -->|"POST /mcp tools/call (Google OIDC ID tokens)"| MCP
    Core -->|"POST /api/jira/issue (Google OIDC ID tokens)"| Bridge
    Bridge -->|"POST /internal/rca-callback (Google OIDC ID tokens)"| Core

    Bridge -->|"HTTPS REST create/comment (Jira Basic auth API token)"| Jira
    Jira -->|"POST /webhook/jira (Shared webhook secret X-Webhook-Token)"| Bridge

    Bridge -->|"Cloud Tasks API enqueue (roles/cloudtasks.enqueuer)"| CT
    CT -->|"POST /investigate (Cloud Tasks OIDC invocation task)"| Agent
    Agent -->|"POST /callback/report (Google OIDC ID tokens)"| Bridge
```

## 3. Security & Authentication Matrix

| Hop / Boundary | Protocol | Credential / Identity | Secret Storage |
|---|---|---|---|
| Slack -> `hitl-chatbot` | HTTPS POST | Slack HMAC (`X-Slack-Signature`) | Secret Manager (`hitl-slack-signing-secret`) |
| `hitl-chatbot` -> Slack | HTTPS REST | Bot token (`xoxb`) | Secret Manager (`hitl-slack-bot-token`) |
| Approval Gate | Block Kit payload | HMAC-SHA256 token (requester-bound) | In-memory / payload (`HMAC_SECRET_KEY`, 600s TTL) |
| `hitl-chatbot` -> Cloud Monitoring | HTTPS REST | Google Cloud IAM / ADC | Compute metadata server |
| `hitl-chatbot` -> `mcp-deploy-context-stub` | HTTPS POST | Google OIDC ID tokens | Compute metadata server (1h TTL) |
| `hitl-chatbot` -> `dib-incident-bridge` | HTTPS POST | Google OIDC ID tokens | Compute metadata server (1h TTL) |
| `dib-incident-bridge` -> Jira | HTTPS REST | Jira Basic auth (API token) | Secret Manager (`jira-api-token`) |
| Jira -> `dib-incident-bridge` | HTTPS POST | Shared webhook secret (`X-Webhook-Token`) | Secret Manager (`jira-webhook-token`) |
| `dib-incident-bridge` -> Cloud Tasks | HTTPS REST | Cloud Tasks IAM (`roles/cloudtasks.enqueuer`) | Workload IAM / metadata server |
| Cloud Tasks -> `dib-sre-agent` | HTTPS POST | Cloud Tasks OIDC invocation task | Cloud Tasks task queue service account |
| `dib-sre-agent` -> `dib-incident-bridge` | HTTPS POST | Google OIDC ID tokens | Compute metadata server (1h TTL) |
| `dib-incident-bridge` -> `hitl-chatbot` | HTTPS POST | Google OIDC ID tokens | Compute metadata server (1h TTL) |

## 4. Execution Sequences

### 4.1 Sequence A: "What changed in the last rollout?"

```mermaid
sequenceDiagram
    actor User as Engineer
    participant Slack as Slack
    participant Bot as hitl-chatbot
    participant MCP as mcp-deploy-context-stub

    User->>Slack: "What changed in the last rollout?"
    Slack->>Bot: POST /slack/events (app_mention)
    Bot-->>Slack: 200 OK (<3s ack)
    Bot->>MCP: POST /mcp tools/call (get_recent_deployments)
    MCP-->>Bot: Deployment rollout metadata
    Bot->>Slack: chat.postMessage (threaded reply)
    Slack-->>User: Render rollout summary in thread
```

### 4.2 Sequence B: "Create JIRA for alert" (includes investigation)

```mermaid
sequenceDiagram
    actor User as Engineer (Requester)
    participant Slack as Slack
    participant Bot as hitl-chatbot
    participant CM as Cloud Monitoring
    participant Bridge as dib-incident-bridge
    participant Jira as Jira
    participant Agent as dib-sre-agent

    User->>Slack: "File JIRA for latest alert"
    Slack->>Bot: POST /slack/events (app_mention)
    Bot-->>Slack: 200 OK (<3s ack)
    Bot->>CM: Resolve active alert policy & incident
    CM-->>Bot: Incident metadata
    Bot->>Slack: Post draft card with Approve button
    Slack-->>User: Render draft card
    User->>Slack: Click Approve
    Slack->>Bot: POST /slack/events (block_actions)
    Bot-->>Slack: 200 OK (<3s ack)
    Note over Bot: Requester check validates HMAC token and user ID
    Bot->>Bridge: POST /api/jira/issue
    Bridge->>Jira: Create issue with callback URL & thread TS
    Jira->>Bridge: Automation rule: POST /webhook/jira
    Bridge-->>Jira: 200 OK
    Note over Bridge,Agent: Bridge enqueues Cloud Tasks which invokes Agent
    Agent->>Bridge: POST /callback/report (RCA findings)
    Bridge->>Jira: Add RCA comment (ADF format)
    Bridge->>Bot: POST /internal/rca-callback (forward RCA)
    Bot->>Slack: chat.postMessage (post RCA to thread)
    Slack-->>User: Render RCA in thread
```

### 4.3 Sequence C: "Update JIRA with RCA"

```mermaid
sequenceDiagram
    actor User as Engineer
    participant Slack as Slack
    participant Bot as hitl-chatbot
    participant Bridge as dib-incident-bridge
    participant Jira as Jira

    User->>Slack: "Update the ticket with this RCA"
    Slack->>Bot: POST /slack/events (app_mention)
    Bot-->>Slack: 200 OK (<3s ack)
    alt Case 1: RCA already attached in thread state
        Note over Bot: Thread state check: rca_attached=True
        Bot->>Slack: chat.postMessage ("RCA already attached")
        Slack-->>User: Render reply
    else Case 2: RCA update required
        Bot->>Slack: Post confirmation card (Approve)
        User->>Slack: Click Approve
        Slack->>Bot: POST /slack/events (block_actions)
        Note over Bot: Requester check validates HMAC token
        Bot->>Bridge: POST /api/jira/issue/{key}/comment
        Bridge->>Jira: Add RCA comment
        Jira-->>Bridge: 200 OK
        Bridge-->>Bot: 200 OK
        Note over Bot: Thread state: rca_attached=True
        Bot->>Slack: chat.postMessage ("Ticket updated with RCA")
        Slack-->>User: Render confirmation
    end
```

## 5. Component Table

| Service | Role | Ingress / Protocol | Identity & Auth |
|---|---|---|---|
| `hitl-chatbot` | Slack conversational interface, intent routing, approval gating, and thread state management | HTTPS POST (`/slack/events`, `/internal/rca-callback`) | Slack HMAC (`X-Slack-Signature`), Google OIDC ID tokens, Bot token (`xoxb`) |
| `mcp-deploy-context-stub` | Deployment rollout context provider implementing Model Context Protocol | HTTPS POST (`/mcp`, `tools/call`) | Google OIDC ID tokens |
| `dib-incident-bridge` | Jira lifecycle management, webhook ingestion, Cloud Tasks dispatch, and RCA callback broker | HTTPS REST (`/api/jira/issue`, `/api/jira/issue/{key}/comment`, status, `/webhook/jira`, `/callback/report`) | Google OIDC ID tokens, Shared webhook secret (`X-Webhook-Token`), Jira Basic auth (API token) |
| `dib-sre-agent` | Multi-specialist investigation engine (triage, specialists, synthesizer, GKE tools) | HTTPS POST (`/investigate`) | Cloud Tasks OIDC invocation task, Google Cloud IAM |
| `Cloud Tasks` | Asynchronous decoupled dispatch queue for agent investigations | Cloud Tasks API / HTTPS | Cloud Tasks IAM (`roles/cloudtasks.enqueuer`), Cloud Tasks OIDC invocation task |
| Jira | Issue tracking, ADF comment rendering, and automation rule dispatch (trigger: label `hitl`) | HTTPS REST API | Jira Basic auth (API token), Shared webhook secret (`X-Webhook-Token`) |

## 6. Edge Cases & Operational Invariants

- **Slack Event Acknowledgment:** Bolt handlers ack all inbound events within 3 seconds; background calls to Cloud Monitoring, MCP, and Bridge run asynchronously after acknowledgment.
- **Approval Gate Security:** Block Kit approval buttons embed a 600s TTL HMAC-SHA256 token signed with requester ID; non-requesters attempting approval receive an ephemeral rejection.
- **Webhook Ingress Verification:** `/webhook/jira` drops payloads failing `X-Webhook-Token` validation with 401 Unauthorized before task queue dispatch.
- **Investigation Decoupling:** Long-running investigations execute out-of-band via Cloud Tasks queues to insulate Jira Automation from agent timeouts.
- **Idempotent In-Thread Updates:** Thread state tracks `rca_attached=True` once an RCA report is delivered to prevent redundant ticket updates on follow-up mentions.

## 7. Reference

- [`slack_arch.md`](../hitl-chatbot/docs/slack_arch.md): Detailed Slack adapter architecture, Bolt request verification, HMAC approval tokens, and thread session state.

## 8. Out of Scope / Future Work

- **Jira MCP Connection:** Exposing Jira through a dedicated Model Context Protocol (MCP) server instead of direct REST API integration from `dib-incident-bridge`.
- **Slack OAuth Integration:** Implementing Slack OAuth v2 installation flow with token rotation and Enterprise Grid organization-level app distribution, replacing the static workspace bot token (`xoxb`).
- **Durable Chat Session State:** Backing thread conversation state with an external durable store (e.g., Cloud Firestore or Cloud SQL) instead of in-memory process memory (`app/session.py`).
- **Slack Socket Mode:** Supporting Slack Socket Mode for local development or private networks without public HTTPS ingress.

## 9. Glossary

- **ADC (Application Default Credentials):** Google Cloud authentication strategy that automatically resolves credentials from local environments or compute metadata servers.
- **ADF (Atlassian Document Format):** Structured JSON representation used by Jira REST APIs for rich-text issue descriptions and comments.
- **Bolt:** Slack's official foundational framework (`slack_bolt`) for building event-driven Slack apps in Python.
- **HITL (Human-In-The-Loop):** Operational workflow where automated triage generates recommendations requiring explicit human engineer approval before mutating external systems.
- **HMAC (Hash-based Message Authentication Code):** Cryptographic verification mechanism (HMAC-SHA256) used for Slack request signatures and single-use, requester-bound approval tokens.
- **IAM (Identity and Access Management):** Google Cloud service defining fine-grained access control and role permissions across cloud resources.
- **MCP (Model Context Protocol):** Open standard protocol for language models and agents to discover tools and query structured operational context.
- **OIDC / Google OIDC ID token:** A Google-signed OpenID Connect identity token presented between Cloud Run services, verified for target audience and caller service account identity.
- **RCA (Root Cause Analysis):** Synthesized technical diagnosis identifying the trigger, impacted components, and remediation steps for an incident.
- **SA (Service Account):** Dedicated Google Cloud IAM identity used by container workloads and Cloud Tasks for automated machine-to-machine authentication.
- **xoxb:** Static workspace-level Slack bot user OAuth token prefix granting permissions defined in the app manifest.
