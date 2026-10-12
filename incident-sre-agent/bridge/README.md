# Incident Bridge Service

The **Incident Bridge** (`incident-bridge`) is a modular, high-reliability gateway connecting incident management platforms (ServiceNow, Jira Cloud, generic webhooks) to the Google Cloud SRE Agent (`sre-agent`).

---

## Architecture

```
[ServiceNow / Jira / Webhook]
             │
             │ Webhook (Bearer / HMAC / Token)
             ▼
   [incident-bridge]  (Cloud Run, Public ingress, per-route auth)
             │
             │ Deterministic Deduplication: inv-<sha256(source:external_id)[:48]>
             │ 900s Dispatch Deadline + OIDC
             ▼
      [Cloud Tasks] (investigations queue)
             │
             ▼
       [sre-agent]  (Multi-agent investigation)
             │
             │ POST /callback/report (OIDC Bearer Auth)
             ▼
   [incident-bridge]
             │
             ├──► Formatted work notes / ADF comments (split if > limit)
             └──► Standalone HTML report attachments
```

---

## Features

1. **Pluggable Adapters (`app/adapters/`)**:
   - `ServiceNowAdapter`: Bearer token auth, Table API ack with `state=2`, Jinja2 HTML work notes with inline styles (`[code]...[/code]`), chunking, and Attachment API integration.
   - `JiraAdapter`: Webhook token auth, comment ack and transition to "In Progress", Atlassian Document Format (ADF) comments, chunking, and Issue Attachment API integration.
   - `GenericAdapter`: HMAC-SHA256 signature verification, normalized incident mapping, in-memory recording.

2. **Deterministic Deduplication (`app/core/tasks.py`)**:
   - Generates deterministic task ID `inv-<sha256(source:external_id)[:48]>`.
   - If a duplicate webhook is received, Cloud Tasks raises `ALREADY_EXISTS` (409) which is dropped gracefully with a 200 OK response.
   - Configures `dispatch_deadline = 900s` (15 minutes).

3. **Secure Callback Auth (`app/core/auth.py`)**:
   - Requires Google-signed OIDC token for `POST /callback/report`.
   - Validates audience matching `CALLBACK_URL` and caller email matching `AGENT_SA` in constant time.
   - Seamless mock token provider support for local testing.

4. **Strict Autoescaped Renderers (`app/templates/`)**:
   - Jinja2 environment with `autoescape=True` ensures all untrusted telemetry and incident descriptions are strictly escaped against XSS and prompt injection.

---

## Local Development & Testing

```bash
# Activate virtual environment
source .venv/bin/activate

# Run tests
pytest -v bridge/tests/

# Run type checker and linter
mypy bridge
ruff check bridge
ruff format --check bridge
```
