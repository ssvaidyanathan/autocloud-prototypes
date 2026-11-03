# Slack Integration Architecture: hitl-chatbot

Slack integrates only with hitl-chatbot. hitl-chatbot calls the MCP stub and Cloud Monitoring directly, and reaches Jira only through dib-incident-bridge, which owns all Jira communication and the investigation pipeline.

Architecture, authorization model, and execution sequences for `hitl-chatbot` Slack integration.

## 1. System Topology

```mermaid
flowchart TD
    SlackEvents["Slack Events API / Interactivity"]
    subgraph CloudRun["Google Cloud Run"]
        subgraph Chatbot["hitl-chatbot"]
            Handler["Bolt AsyncSlackRequestHandler\n(verifies X-Slack-Signature + timestamp; acks <3s)"]
            Router["Event / Action Router\n(app_mention, message, block_actions)"]
            Core["Agent Runner / Approval Gate / Thread State\n(in-process: agent.py, tokens.py, session.py)"]
            Client["Bolt AsyncWebClient\n(slack_sdk, via Bolt)"]
            Handler --> Router --> Core --> Client
        end
    end
    SlackWeb["Slack Web API\n(message appears in thread)"]
    SM["Secret Manager"]
    Downstream["Downstream (out of scope for this doc)"]

    SlackEvents -->|"POST /slack/events"| Handler
    Client -->|"HTTPS POST Bearer xoxb\n(chat.postMessage / chat.update)"| SlackWeb
    SM -.->|"hitl-slack-signing-secret"| Handler
    SM -.->|"hitl-slack-bot-token"| Client
    Core <-->|"Internal APIs / Callbacks"| Downstream
```

## 2. Authorization/Authentication

Inbound Slack requests use HMAC-SHA256 request signing (`X-Slack-Signature`), not OAuth. Outbound API calls to Slack use a workspace bot token (`xoxb`) without user delegation. Interactive approvals use single-use HMAC-SHA256 tokens bound to the requester ID. Downstream service communication uses Google Cloud IAM OIDC (out of scope for this document).

- **OAuth Status:** Static workspace bot token (`xoxb`) is used; full Slack OAuth v2 install flow with token rotation is not required for single-workspace deployment.

| Hop | Protocol | Credential | Storage | Rotation |
|---|---|---|---|---|
| Slack -> Bot | HTTPS | HMAC-SHA256 (`X-Slack-Signature`) | Secret Manager (`hitl-slack-signing-secret`) | Slack console re-issue |
| Bot -> Slack | HTTPS REST | Bot token (`xoxb`) | Secret Manager (`hitl-slack-bot-token`) | Slack app reinstall |
| Approval Gate | Block Kit payload | HMAC-SHA256 token (requester-bound) | Memory / Block Kit payload | `HMAC_SECRET_KEY` (600s TTL) |
| Bot <-> Downstream | HTTPS POST | Google Cloud IAM OIDC ID Token | Metadata Server (1h TTL) | Automatic (GCP IAM) |

## 3. Sequence A: Rollout Query

```mermaid
sequenceDiagram
    actor Eng as Engineer
    participant Slack as Slack
    participant Bot as hitl-chatbot (Bolt)
    participant Backend as hitl-chatbot backend

    Eng->>Slack: Ask rollout changes
    Slack->>Bot: POST /slack/events (app_mention)
    Bot-->>Slack: 200 OK (<3s ack)
    Bot->>Backend: Execute intent (LOOKUP_DEPLOYMENTS)
    Backend-->>Bot: Deployment metadata
    Bot->>Slack: chat.postMessage (thread reply)
    Slack-->>Eng: Render rollout summary
```

## 4. Sequence B: Alert Ticket & RCA Delivery

```mermaid
sequenceDiagram
    actor Eng as Engineer (Requester)
    participant Slack as Slack
    participant Bot as hitl-chatbot (Bolt)
    participant Backend as hitl-chatbot backend

    Eng->>Slack: File ticket for alert
    Slack->>Bot: POST /slack/events (app_mention)
    Bot-->>Slack: 200 OK (<3s ack)
    Bot->>Backend: Resolve alert policy
    Backend-->>Bot: Policy metadata
    Bot->>Slack: chat.postMessage (Draft card with Approve/Cancel buttons & HMAC token)
    Eng->>Slack: Click [Approve]
    Slack->>Bot: POST /slack/events (hitl_approve_ticket)
    Bot-->>Slack: 200 OK (<3s ack)
    Note over Bot: Requester check (verify HMAC token & requester ID)
    Bot->>Backend: Create ticket & trigger investigation
    Backend-->>Bot: Ticket created
    Bot->>Slack: chat.update (Replace buttons with confirmation)
    Backend->>Bot: POST /internal/rca-callback (RCA dossier)
    Bot->>Slack: chat.postMessage (RCA posted to incident thread)
    Slack-->>Eng: Render RCA in thread
```

## 5. Sequence C: In-Thread Ticket Update

```mermaid
sequenceDiagram
    actor Eng as Engineer
    participant Slack as Slack
    participant Bot as hitl-chatbot (Bolt)
    participant Store as Thread State (session.py)

    Eng->>Slack: Update ticket with RCA
    Slack->>Bot: POST /slack/events (app_mention)
    Bot-->>Slack: 200 OK (<3s ack)
    Bot->>Store: get_active_ticket(channel:thread_ts)
    Store-->>Bot: Ticket context (rca_attached=True)
    Bot->>Slack: chat.postMessage (Thread reply: RCA already attached)
    Slack-->>Eng: Render reply
```

## 6. Edge Cases & Failure Modes

- **Invalid Signature (401):** Requests with missing, invalid, or >5m timestamp HMAC signature (`X-Slack-Signature`) return 401.
- **Non-Requester (403):** If approver ID != `requester_id` in approval token, returns ephemeral 403; draft remains valid for requester.
- **Duplicate Ticket Warning:** In threads with an active ticket, ticket creation intents inject a warning to update the existing ticket.
- **Callback Network Fallback:** If `/internal/rca-callback` fails, RCA is retained downstream and can be queried via backend API.
