# Incident Bridge Human-In-The-Loop (HITL) Endpoints

This document specifies the Human-In-The-Loop (HITL) endpoints, Atlassian Document Format (ADF) template engine, Google OIDC authentication, and metadata persistence introduced in the Dev-In-A-Box incident bridge (`dib-incident-bridge`).

---

## 1. Overview & Architecture

The incident bridge acts as the bidirectional integration layer between external collaboration platforms (such as the HITL Chatbot or Jira Cloud) and the autonomous SRE reasoning engine.

In this architecture:
1. **Interactive Creation:** The HITL Chatbot issues an authenticated request to `POST /api/jira/issue` to file an incident ticket once human approval is confirmed.
2. **ADF Template Ingestion:** The bridge renders ticket descriptions into Atlassian Document Format (ADF) using standard or customized incident templates.
3. **Metadata Storage:** Chat thread references and webhook callback URLs are attached to the Jira issue via Jira Cloud Issue Properties (`hitl_metadata`).
4. **Autonomous Investigation:** The bridge enqueues the investigation task to Cloud Tasks for asynchronous execution by `dib-sre-agent`.
5. **Automatic RCA Forwarding:** Upon completion, the bridge fetches a Google Cloud OIDC ID token and posts the Root Cause Analysis (RCA) dossier back to the chatbot callback endpoint.

---

## 2. API Endpoints

### 2.1. Create Jira Issue with Template
- **Method & Route:** `POST /api/jira/issue`
- **Authentication:** Google Cloud OIDC ID Token (`Authorization: Bearer <token>`) or internal service auth.
- **Request Payload:**
  ```json
  {
    "summary": "Alert violation on snow-ip-healthcheck-3",
    "description": "Optional raw description or markdown body",
    "template_data": {
      "detection": "- Started At: 2026-10-30T20:02:58Z\n- Target IP: 10.10.0.1",
      "impact": "- Service healthcheck failing across 3 probes",
      "trigger": "- Alert policy snow-ip-healthcheck-3 violated",
      "mitigation": "- Investigating recent workload rollouts"
    },
    "issue_properties": {
      "hitl_metadata": {
        "slack_thread": "C01234567:1730318578.000100",
        "callback_url": "https://hitl-chatbot-service-url/internal/rca-callback",
        "alert_started_at": "2026-10-30T20:02:58Z"
      }
    },
    "labels": ["hitl-incident", "alert-open-2026-10-30t20-02-58z"]
  }
  ```
- **Response (`201 Created`):**
  ```json
  {
    "key": "OPS-301",
    "id": "10301",
    "self": "https://your-domain.atlassian.net/rest/api/3/issue/10301",
    "status": "created"
  }
  ```

### 2.2. Get Issue & Investigation Status
- **Method & Route:** `GET /api/jira/issue/{key}/status`
- **Authentication:** Google Cloud OIDC ID Token
- **Response (`200 OK`):**
  ```json
  {
    "key": "OPS-301",
    "status": "investigating",
    "opened_at": "2026-10-30T20:02:58Z",
    "rca_attached": false
  }
  ```

### 2.3. Add Issue Comment
- **Method & Route:** `POST /api/jira/issue/{key}/comment`
- **Authentication:** Google Cloud OIDC ID Token (`Authorization: Bearer <token>`) from authorized caller (`hitl-chatbot-sa` or agent SA).
- **Request Payload:**
  ```json
  {
    "body": "Root Cause Analysis findings attached: Service degraded due to replica scale down.",
    "properties": {
      "comment_source": "hitl-chatbot",
      "action": "rca_attachment"
    }
  }
  ```
- **Response (`201 Created`):**
  ```json
  {
    "status": "created",
    "issue_key": "OPS-301",
    "comment_id": "10042",
    "comment": {
      "id": "10042",
      "self": "https://your-domain.atlassian.net/rest/api/3/issue/10301/comment/10042"
    }
  }
  ```
- **Error Handling:**
  - `400 Bad Request`: Missing issue key or empty comment body.
  - `401 Unauthorized`: Missing or invalid OIDC token.
  - `403 Forbidden`: Token issuer/caller service account is not in `allowed_emails`.
  - `404 Not Found`: Target issue `{key}` does not exist in Jira.
  - `502 Bad Gateway`: Upstream Jira Cloud API failure or network timeout.

---

## 3. Jira Issue Properties (`hitl_metadata`)

Jira Cloud issue properties store arbitrary JSON metadata without requiring custom field configuration:
- **Property Key:** `hitl_metadata`
- **Fields:**
  - `slack_thread`: Thread identifier for sending async updates back to the requesting user.
  - `callback_url`: Chatbot endpoint invoked when RCA is ready.
  - `alert_started_at`: Original ISO 8601 violation timestamp from Cloud Monitoring.
  - `requester_id`: User ID of the human who approved the action.

---

## 4. Automatic RCA Forwarding with OIDC

When `dib-sre-agent` completes its multi-specialist investigation, the bridge automatically forwards the results:
1. Inspects the issue's `hitl_metadata.callback_url`.
2. Obtains a Google Cloud OIDC ID token with audience matching `callback_url` using `google.auth.transport.requests.Request` and `google.oauth2.id_token.fetch_id_token`.
3. Sends an HTTP `POST` request with:
   ```json
   {
     "ticket_key": "OPS-301",
     "thread_ref": "C01234567:1730318578.000100",
     "report": {
       "root_cause": "Workload replica scale-down event triggered 503 errors.",
       "confidence": 0.65,
       "timeline": [...],
       "recommended_actions": [...]
     }
   }
   ```
4. Verifies HTTP 200 response from the chatbot receiver (`/internal/rca-callback`).

---

## 5. Template Loading: Bundled Generic & GCS Loader

The bridge formats tickets using Atlassian Document Format (ADF) generated from Markdown templates:
- **Bundled Generic Template (`app/templates/incident_template.md`):** Included in the repository with zero customer or environment identifiers. Provides clean sections: `Detection`, `Impact`, `Trigger`, `Mitigation`, and `Root Causes`.
- **Dynamic GCS Loading:** The environment variable `TICKET_TEMPLATE_PATH` supports loading custom templates dynamically:
  ```bash
  # Local template (default)
  TICKET_TEMPLATE_PATH="app/templates/incident_template.md"

  # Dynamic customer template in Cloud Storage
  TICKET_TEMPLATE_PATH="gs://your-bucket/templates/custom_incident_ticket.md"
  ```
- **ADF Converter (`app/templates/jira_adf.py`):** Converts markdown headings, paragraphs, bullet points, code blocks, and tables into valid Jira Cloud REST API v3 ADF documents.

---

## 6. Alert `opened_at` Propagation & SRE Agent Fixes

### 6.1. `opened_at` Violation Timestamp Propagation
- Cloud Monitoring violations specify the exact time an anomaly started (`alert_started_at`).
- The bridge propagates this timestamp into `hitl_metadata` and the Jira description.
- When `jira.py` extracts the incident object for investigation, it parses `alert_started_at` as the authoritative `Incident.opened_at` rather than the Jira ticket creation timestamp.
- **Architectural Impact:** This ensures the 15-minute triage lookback window (`start_time = opened_at - 15m`) aligns with the actual outage onset rather than when a human filed the ticket.

### 6.2. DIB SRE Agent Fixes
- **Synthesizer Crash Fix:** In `agent/app/synthesizer.py`, safely parses `prior_incidents` timeline items whether they are formatted as dictionary objects, timestamped strings, or unparsed text.
- **Root Logging Configuration:** In `agent/app/main.py`, calls `logging.basicConfig(level=logging.INFO)` at startup to ensure all agent diagnostic and specialist reasoning logs are captured in Cloud Logging.
- **15-Minute Lookback Window:** In `agent/app/triage.py`, strictly preserves the architectural contract lookback window of 15 minutes before incident open.
