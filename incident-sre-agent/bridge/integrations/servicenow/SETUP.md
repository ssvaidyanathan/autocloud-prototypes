# ServiceNow Integration Setup Guide

This guide describes how to configure ServiceNow Flow Designer and REST steps to automatically trigger the Google Cloud SRE Agent upon incident creation, receive real-time progress acknowledgement, and consume automated diagnostic reports.

---

## 1. Architectural Overview & Request Flow

When an incident is created or updated to an eligible severity in ServiceNow:
1. **Flow Designer** executes a trigger matching incident criteria.
2. An **Outbound REST Action** delivers the incident payload to Incident Bridge at `POST /webhook/servicenow`.
3. **Incident Bridge** validates the bearer token, acknowledges the incident, sets the state to `In Progress` (`state=2`), and posts an initial work note.
4. **SRE Agent** conducts autonomous investigation across GCP telemetry and posts back the final report.
5. **Incident Bridge** updates the incident with an HTML work note (using `[code]...[/code]`) and attaches a comprehensive HTML diagnostic report (`sre_investigation_report_INCxxxxxx.html`).

---

## 2. Prerequisites

1. **Active ServiceNow Instance**: Enterprise or Developer instance, Vancouver release or later (verified on Brazil).
   - For a Personal Developer Instance (PDI), open it from the developer portal and sign in as `admin`. The admin password is under the portal's profile menu → **Manage instance password**.
2. **Dedicated Service Account**:
   - Username: `svc_sre_agent`
   - Roles: `itil` (permits updating incidents, adding work notes, and attaching files).
   - Flag: Check **Internal Integration User** on the sys_user record (called **Web service access only** on older releases).
   - **Identity type:** `Machine`. Without it, newer releases deny every basic-auth API call for the user, so the bridge gets `401` on the acknowledgement and on the report write-back. The instance's System Log shows `SNCRestrictBasicAuthUserAuthenticationGate: denied basic-auth API call for interactive-login user`.
   - **Set Password** ticks **Password needs reset** ("Require password change on next login") by default. Untick it, or API sign-in fails.
3. **System Property Configuration**:
   - Verify `glide.ui.security.allow_codetag` is `true` under `sys_properties.list`. It usually already exists and is `true` by default, so this is just a check. It allows work notes containing `[code]...[/code]` tags to render formatted HTML.
4. **IntegrationHub**: the REST step in Step 2 needs the IntegrationHub plugin. On a PDI, activate **IntegrationHub Starter Pack Installer**; the larger packs are not needed. Without it, the Workflow Studio step picker shows only "To enable Action Steps for integrations, the IntegrationHub plugin is required" under Integrations.
5. **Deployed Incident Bridge URL**:
   - Public Cloud Run URL, e.g. `https://incident-bridge-xyz-uc.a.run.app`.
6. **Shared Secret / Webhook Token**:
   - High-entropy bearer token stored in Secret Manager under `bridge-servicenow-webhook-token`. The webhook accepts only `Authorization: Bearer <token>`.
   - `bridge-servicenow-username` and `bridge-servicenow-password` are not used for the webhook. The bridge uses them (HTTP Basic) for its own outbound calls to the ServiceNow REST API, as `svc_sre_agent`.

---

## 3. Field Mapping Reference

The Incident Bridge normalizes incoming ServiceNow payloads into the canonical `Incident` model:

| ServiceNow Field | Payload JSON Key | Canonical Field | Type | Description / Transformation |
|---|---|---|---|---|
| `sys_id` | `sys_id` | `incident_id` | `str` | Unique 32-character record identifier |
| `number` | `number` | `title` prefix | `str` | Incident ticket identifier (e.g. `INC0010001`) |
| `short_description`| `short_description`| `title` | `str` | Brief incident summary |
| `description` | `description` | `description` | `str` | Full incident details and customer symptom report |
| `priority` | `priority` | `severity` | `str` | Normalized: `1` → `critical`, `2` → `high`, `3` → `medium`, `4`/`5` → `low` |
| `urgency` | `urgency` | (metadata) | `str`/`int` | Incident urgency |
| `impact` | `impact` | (metadata) | `str`/`int` | Incident impact |
| `category` | `category` | (metadata) | `str` | Categorization (e.g., `Software`, `Network`, `Cloud`) |
| `cmdb_ci` | `cmdb_ci` | (metadata) | `str` | Affected Configuration Item or service name |
| `caller_id` | `caller_id` | (metadata) | `str` | Caller user sys_id or display name |
| `opened_at` | `opened_at` | `opened_at` | `datetime` | UTC ISO-8601 timestamp (e.g. `2026-10-09T04:30:00Z` or `YYYY-MM-DD HH:MM:SS`) |

---

## 4. Step-by-Step Flow Designer Configuration

### Step 1: Create Connection & Credential Alias

1. Create the credential first. Navigate to **Connections & Credentials > Credentials** (`discovery_credentials.list`), click **New**, and select **API Key Credentials**:
   - **Name:** `IncidentBridgeWebhookToken`
   - **API Key:** `<your-servicenow-webhook-token>`
   - **API Key Header:** `Authorization`
   - **API Key Prefix:** `Bearer ` (with a trailing space)
   - With these set, ServiceNow sends `Authorization: Bearer <token>` itself, so the token does not appear in plain text in the REST step. Older releases without these fields need a manual `Authorization` header in the REST step instead (see Step 2).
   - Click **Submit**.
2. Navigate to **Connections & Credentials > Connection & Credential Aliases** and click **New**:
   - **Name:** `IncidentBridgeAlias`
   - **Type:** `Connection & Credential`
   - **Connection Type:** `HTTP`
   - **Configuration Template:** optional; leave it blank and ignore the hint box next to it.
3. Click **Submit**, then reopen the alias. Its tabs are **Connections**, **Connection Attributes** and **Child Aliases** (newer releases no longer have a **Credentials** related list here).
4. On the **Connections** tab, click **New** and choose **HTTP(s) Connection**:
   - **Name:** `IncidentBridgeConnection`
   - **Credential:** Select `IncidentBridgeWebhookToken`
   - **Connection URL:** `https://<your-bridge-url>`, the bare bridge origin with no trailing slash or path. The REST step adds `/webhook/servicenow`.
   - Click **Submit**.

### Step 2: Create Custom Flow Designer Action

1. Navigate to **Process Automation > Workflow Studio** (called **Flow Designer** on older releases).
2. Click **New > Action**:
   - **Action Name:** `Trigger GCP SRE Agent Investigation`
   - **Application:** `Global`
   - **Category:** `Incident Management`
3. Define **Inputs**:
   - `incident_sys_id` (String, Mandatory). `sys_id` is a restricted input name; saving fails with "Invalid input name(s) 'sys_id' containing restricted keyword(s)".
   - `number` (String, Mandatory)
   - `short_description` (String, Mandatory)
   - `description` (String)
   - `priority` (String)
   - `urgency` (String)
   - `impact` (String)
   - `category` (String)
   - `cmdb_ci` (String)
   - `caller_id` (String)
   - `opened_at` (String)
4. Add a **REST Step**:
   - **Connection:** Select `Use Connection Alias` → `IncidentBridgeAlias`
   - **Build Request:** `Manually`
   - **HTTP Method:** `POST`
   - **Resource Path:** `/webhook/servicenow`
   - **HTTP Headers:**
     - `Content-Type`: `application/json`
     - Only on releases whose API Key credential has no Header/Prefix fields: `Authorization`: `Bearer <your-servicenow-webhook-token>`
   - **Request Body (JSON):**
     ```json
     {
       "sys_id": "{{inputs.incident_sys_id}}",
       "number": "{{inputs.number}}",
       "short_description": "{{inputs.short_description}}",
       "description": "{{inputs.description}}",
       "priority": "{{inputs.priority}}",
       "urgency": "{{inputs.urgency}}",
       "impact": "{{inputs.impact}}",
       "category": "{{inputs.category}}",
       "cmdb_ci": "{{inputs.cmdb_ci}}",
       "caller_id": "{{inputs.caller_id}}",
       "opened_at": "{{inputs.opened_at}}"
     }
     ```
     The `{{inputs.x}}` placeholders are illustrative. In the editor, type the JSON and drag the matching input data pill into each value. String pills are inserted as-is, not JSON-escaped, so a quote, backslash or newline in a field such as `description` produces invalid JSON and the bridge rejects the request.
5. Click **Save** and **Publish**.

### Step 3: Create Trigger Flow

1. In **Process Automation > Workflow Studio**, click **New > Flow**:
   - **Flow Name:** `Incident - Automated SRE Investigation`
   - **Run As:** `System User`
2. **Trigger**:
   - **Trigger Type:** `Created`
   - **Table:** `Incident [incident]`
   - **Condition:** `Priority` is one of `1 - Critical`, `2 - High`, `3 - Moderate`. (A "less than or is" condition with a typed value did not save in the condition builder.)
3. **Actions**:
   - Click **Add an Action** → select `Trigger GCP SRE Agent Investigation`.
   - Map each action input with the data-pill picker (**Trigger - Record Created → Incident Record → field**). Typing `{{trigger.current.x}}` text instead produces pills with blank labels. Configuration item and Caller map as reference pills.
     - `incident_sys_id` ← `Trigger -> Incident Record -> Sys ID`
     - `number` ← `Trigger -> Incident Record -> Number`
     - `short_description` ← `Trigger -> Incident Record -> Short description`
     - `description` ← `Trigger -> Incident Record -> Description`
     - `priority` ← `Trigger -> Incident Record -> Priority`
     - `urgency` ← `Trigger -> Incident Record -> Urgency`
     - `impact` ← `Trigger -> Incident Record -> Impact`
     - `category` ← `Trigger -> Incident Record -> Category`
     - `cmdb_ci` ← `Trigger -> Incident Record -> Configuration item`
     - `caller_id` ← `Trigger -> Incident Record -> Caller -> Name`
     - `opened_at` ← `Trigger -> Incident Record -> Opened`
4. Click **Save** and **Activate**. On a slow PDI the flow can run anywhere from a few seconds to about 4 minutes after the incident is created.

---

## 5. Sample Test Payloads & Manual Verification

You can verify Incident Bridge webhook ingestion directly using `curl`:

### Bearer Token Authentication
```bash
curl -X POST "https://<your-bridge-url>/webhook/servicenow" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <your-servicenow-webhook-token>" \
  -d '{
    "sys_id": "a1b2c3d4e5f67890123456789abcdef0",
    "number": "INC0019482",
    "short_description": "Elevated 500 errors on shop-api checkout",
    "description": "Customers report 500 Internal Server Error when completing checkout on shop-api.",
    "priority": "1",
    "urgency": "1",
    "impact": "1",
    "category": "Software",
    "cmdb_ci": "shop-api",
    "caller_id": "admin",
    "opened_at": "2026-10-09 05:00:00"
  }'
```

The webhook does not accept HTTP Basic authentication; a request without a valid `Authorization: Bearer` header gets `401`.

### Expected Response
```json
{
  "status": "accepted",
  "incident_id": "a1b2c3d4e5f67890123456789abcdef0",
  "task_name": "inv-9f83...c2d",
  "ticket_acknowledged": true
}
```

---

## 6. End-to-End Verification Checklist

1. **Immediate Acknowledgment (within 5 seconds)**:
   - Incident state transitions to `In Progress` (`state = 2`).
   - A work note is added:
     ```text
     SRE Agent: Investigation underway. Findings will be posted within ~10 minutes.
     ```
2. **Investigation Completion (typically 3–8 minutes)**:
   - A rich HTML work note is appended detailing:
     - Executive summary and incident classification
     - Root cause hypothesis with high/medium confidence
     - Diagnostic timeline reconstructed from telemetry
     - Direct log snippets and metric anomalies
     - Actionable remediation runbook recommendations
   - An HTML report attachment is uploaded to the incident record:
     `sre_investigation_report_INC0019482.html`.

---

## 7. Troubleshooting & Common Pitfalls

### 1. Work Notes Display Raw HTML Tags
- **Cause**: ServiceNow property `glide.ui.security.allow_codetag` is disabled.
- **Fix**: Navigate to `sys_properties.list`, search for `glide.ui.security.allow_codetag`, set the value to `true`, and save. Incident Bridge wraps all HTML work notes in `[code]...[/code]`.

### 2. HTTP 401 Unauthorized / 403 Forbidden
- **Cause**: Webhook token mismatch or missing credentials in Secret Manager.
- **Fix**: Check `SERVICENOW_WEBHOOK_TOKEN` in Secret Manager. Ensure the Authorization header matches: `Authorization: Bearer <token>`. Basic auth is not accepted on the webhook. `SERVICENOW_USERNAME` and `SERVICENOW_PASSWORD` only matter if the bridge's own calls back to ServiceNow (acknowledgement, work notes, attachments) fail.

### 3. Outbound REST Step Fails with Connection Refused or Timeout
- **Cause**: Network egress restrictions or invalid Incident Bridge URL.
- **Fix**: Ensure ServiceNow MID Server is configured if instance is restricted, or verify that the Cloud Run service URL is accessible over HTTPS (port 443) and ingress is set to `INGRESS_TRAFFIC_ALL`.

### 4. Work Note Truncation
- **Cause**: ServiceNow journal field (`work_notes`) maximum string length.
- **Fix**: Incident Bridge automatically formats a concise executive summary for the journal note and attaches the complete, full-fidelity investigation dossier as an HTML file attachment.

### 5. Attachment Upload Fails (HTTP 403 on ServiceNow Attachment API)
- **Cause**: The service account `svc_sre_agent` lacks permission to insert records into `sys_attachment`.
- **Fix**: Ensure `svc_sre_agent` has the `itil` role and ACLs on `sys_attachment` allow `create` access for `itil`.
