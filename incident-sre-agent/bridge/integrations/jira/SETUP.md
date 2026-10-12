# Jira Cloud Integration Setup Guide

This guide describes how to configure Jira Cloud Automation to automatically trigger the Google Cloud SRE Agent upon issue creation, receive real-time progress acknowledgement, and consume automated diagnostic reports formatted in Atlassian Document Format (ADF).

---

## 1. Architectural Overview & Request Flow

When an incident issue is created or updated in Jira Cloud:
1. **Jira Automation** intercepts the event and executes a **Send web request** action to `POST /webhook/jira`.
2. **Incident Bridge** validates the webhook token (`X-Webhook-Token` or `Authorization: Bearer <token>`), acknowledges the issue, transitions its status to `In Progress`, and adds an initial acknowledgment comment.
3. **Cloud Tasks** enqueues a deduplicated task to invoke the private SRE Agent.
4. **SRE Agent** conducts telemetry investigation and reports findings back to the bridge callback endpoint.
5. **Incident Bridge** posts a rich ADF comment (with color-coded severity panels, timeline bullet points, and monospace telemetry snippets) and uploads the complete HTML diagnostic report (`sre_investigation_<KEY>.html`) as an attachment.

---

## 2. Prerequisites

1. **Jira Cloud Site**: An active Jira Software or Jira Service Management Cloud instance (e.g. `https://<your-domain>.atlassian.net`).
2. **Dedicated Service Account User**:
   - Email: `sre-agent@yourdomain.com`
   - Permissions: Project access with permission to **Browse Projects**, **Add Comments**, **Create Attachments**, and **Transition Issues**.
3. **Atlassian API Token**:
   - Generate an API token at: [Atlassian API Tokens](https://id.atlassian.com/manage-profile/security/api-tokens).
   - Store email in Secret Manager under `bridge-jira-email`.
   - Store token in Secret Manager under `bridge-jira-api-token`.
4. **Deployed Incident Bridge URL**:
   - Public Cloud Run URL, e.g. `https://incident-bridge-xyz-uc.a.run.app`.
5. **Shared Webhook Secret Token**:
   - High-entropy secret token stored in Secret Manager under `bridge-jira-webhook-token`.

---

## 3. Field Mapping Reference

The Incident Bridge normalizes incoming Jira Cloud payloads into the canonical `Incident` model:

| Jira Cloud Field | Smart Value | Canonical Field | Type | Description / Transformation |
|---|---|---|---|---|
| `issue.id` | `{{issue.id}}` | `incident_id` | `str` | Internal numeric Jira issue ID |
| `issue.key` | `{{issue.key}}` | `title` prefix | `str` | Human-readable key (e.g. `OPS-1042`, `INC-402`) |
| `issue.fields.summary` | `"{{issue.summary.jsonEncode}}"` | `title` | `str` | Issue summary headline |
| `issue.fields.description`| `"{{issue.description.jsonEncode}}"`| `description`| `str` | Detailed customer symptom report |
| `issue.fields.priority.name`| `{{issue.priority.name}}` | `severity` | `str` | Normalized: `Highest` → `critical`, `High` → `high`, `Medium` → `medium`, `Low`/`Lowest` → `low` |
| `issue.created` | `{{issue.created}}` | `opened_at` | `datetime` | UTC ISO-8601 creation timestamp |
| `issue.fields.reporter`| `"{{issue.reporter.displayName.jsonEncode}}"` | (metadata) | `str` | Reporter name |

---

## 4. Step-by-Step Jira Cloud Automation Rule

### Step 1: Create Automation Rule

1. In Jira, navigate to your Project Settings:
   - **Project settings > Automation** (or **Jira settings > System > Global automation**).
2. Click **Create rule** in the top-right corner.

### Step 2: Configure Trigger
1. Select trigger **Issue created**.
2. Click **Save**.

### Step 3: Add Issue Condition (Optional but Recommended)
1. Click **Add component > Issue fields condition**:
   - **Field:** `Issue Type`
   - **Condition:** `equals`
   - **Value:** `Incident` (or `Bug`, `Problem`)
2. (Optional) Add condition on `Priority`: `Priority` is one of `Highest`, `High`, `Medium`.
3. Click **Save**.

### Step 4: Configure Webhook Action
1. Click **Add component > New action > Send web request**.
2. Configure web request properties:
   - **Web request URL:** `https://<your-bridge-url>/webhook/jira`
   - **HTTP method:** `POST`
   - **Web request headers:**
     - `Content-Type`: `application/json`
     - `X-Webhook-Token`: `<your-jira-webhook-token>`
   - **Web request body:** Select `Custom data`.
   - **Custom data content:**
     ```json
     {
       "issue": {
         "id": "{{issue.id}}",
         "key": "{{issue.key}}",
         "fields": {
           "summary": "{{issue.summary.jsonEncode}}",
           "description": "{{issue.description.jsonEncode}}",
           "priority": {
             "name": "{{issue.priority.name}}"
           },
           "created": "{{issue.created}}",
           "reporter": {
             "displayName": "{{issue.reporter.displayName.jsonEncode}}"
           }
         }
       }
     }
     ```
   > [!IMPORTANT]
   > Notice that `summary`, `description`, and `reporter.displayName` use `"{{...jsonEncode}}"` with surrounding quotes. The `.jsonEncode` smart value helper properly escapes internal quotes, tabs, and newlines for valid JSON strings.
   >
   > `labels` is dropped from the custom data payload because the Incident Bridge Jira adapter automatically defaults missing labels to `[]`.

   > [!WARNING]
   > Smart quotes or curly quotes (e.g. copied from word processors or rich text like `“` and `”`) will cause invalid JSON in Jira automation; standard straight quotes (`"`) must be used throughout the custom data payload.

3. Check **Wait for response** (optional: set timeout to 10 seconds).
4. Click **Save**.

### Step 5: Name and Enable the Rule
1. Name the rule: `GCP SRE Agent - Automated Triage & Investigation`.
2. Click **Turn on rule**.

---

## 5. Workflow Transition Configuration

When an incident is acknowledged, the bridge automatically attempts to transition the Jira issue to an "In Progress" status.

1. **Verify Jira Workflow Statuses**:
   - Go to **Project Settings > Workflows** and view the active workflow for the Incident issue type.
   - Note the transition name or target status name (e.g. `In Progress`, `Investigating`, `Under Investigation`).
2. **Bridge Configuration**:
   - By default, the bridge looks for a transition named `In Progress`.
   - If your workflow uses a different transition name, set the environment variable on Incident Bridge:
     ```bash
     JIRA_IN_PROGRESS_TRANSITION="Investigating"
     ```
3. **Workflow Permission / Global Transitions**:
   - Ensure the workflow allows transition from the initial state (e.g. `Open` or `To Do`) to the target state. Adding an "All" transition (allowing transitions from any status) ensures the bridge never encounters transition blockers.

---

## 6. Sample Test Payloads & Manual Verification

You can verify Incident Bridge webhook ingestion directly using `curl`:

### Testing with Header Token (`X-Webhook-Token`)
```bash
curl -X POST "https://<your-bridge-url>/webhook/jira" \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Token: <your-jira-webhook-token>" \
  -d '{
    "issue": {
      "id": "10492",
      "key": "OPS-384",
      "fields": {
        "summary": "Elevated 500 errors on shop-api checkout",
        "description": "Customer transactions are failing with HTTP 500 during checkout step.",
        "priority": {
          "name": "High"
        },
        "created": "2026-10-09T05:00:00.000+0000",
        "reporter": {
          "displayName": "SRE On-Call"
        },
        "labels": ["shop-api", "production", "incident"]
      }
    }
  }'
```

### Expected Response
```json
{
  "status": "accepted",
  "incident_id": "10492",
  "task_name": "inv-7e3a...b1f",
  "ticket_acknowledged": true
}
```

---

## 7. End-to-End Verification Checklist

1. **Immediate Acknowledgment (within 5 seconds)**:
   - Issue status transitions to `In Progress`.
   - A comment is posted to the Jira issue:
     ```text
     SRE Agent: Investigation underway. Findings will be posted within ~10 minutes.
     ```
2. **Investigation Completion (typically 3–8 minutes)**:
   - A rich Atlassian Document Format (ADF) comment is posted featuring:
     - An alert panel with the root cause hypothesis and severity
     - A structured summary table of identified anomalies
     - Monospaced code blocks with log trace snippets and error counts
     - Next-step remediation playbooks from the SRE runbook
   - An HTML report attachment is uploaded to the Jira issue:
     `sre_investigation_OPS-384.html`.

---

## 8. Troubleshooting & Common Pitfalls

### 1. Jira Automation Fails with "Invalid JSON payload"
- **Cause**: Special characters, quotes, or multiline text in the issue summary or description broke the JSON formatting, or smart/curly quotes were copied into the template.
- **Fix**: Use quoted `.jsonEncode` smart-value helpers (`"{{issue.summary.jsonEncode}}"`, `"{{issue.description.jsonEncode}}"`, `"{{issue.reporter.displayName.jsonEncode}}"`) and verify that standard straight ASCII double quotes (`"`) are used instead of smart or curly quotes.

### 2. HTTP 401 Unauthorized
- **Cause**: The `X-Webhook-Token` sent by Jira does not match `bridge-jira-webhook-token` in Secret Manager.
- **Fix**: Verify the secret value in Google Secret Manager and update the webhook header in Jira Automation.

### 3. Comment Posted but Status Transition Fails
- **Cause**: Jira workflow does not allow transitioning from the current state to the configured `In Progress` status name, or the service account lacks the "Transition Issues" permission.
- **Fix**: Review workflow transitions in Jira Project Settings. Check if `JIRA_IN_PROGRESS_TRANSITION` needs to be customized to match your exact workflow transition label.

### 4. Attachment Upload Fails (HTTP 403 or 413)
- **Cause**: Service account lacks "Create Attachments" permission, or the attachment exceeds Jira instance attachment limits.
- **Fix**: Check Project Settings > Permissions > Create Attachments. Incident Bridge HTML reports are typically under 100 KB, well within standard Jira limits.

---

## 9. Jira Cloud Free Plan Considerations & Nuances

When evaluating or running the Incident SRE Agent against Jira Cloud Free tier instances, take note of the following platform nuances:

1. **Monthly Automation Rule Execution Quota**:
   - Jira Free plans include a strict limit of **100 rule executions per month** across the entire Jira Cloud site.
   - Every incident created triggers the automation rule that dispatches the webhook. In test environments or high-volume scenario evaluations, rule execution counts can be exhausted rapidly, causing Jira to throttle or suspend automation rules until the next billing cycle reset.
   - *Recommendation*: Use project-scoped rules and restrict rule triggers with issue conditions (`Issue Type = Incident`) to avoid burning execution quota on unrelated issues.

2. **Single-Project Scoping vs. Global Rules**:
   - Jira Free restricts global multi-project automation rules. Always configure the automation rule inside **Project Settings > Automation** within your target project rather than under Jira Global Administration.

3. **Default Simplified Workflows & Transition Names**:
   - Free plan projects typically utilize simplified Team-Managed or Company-Managed default workflows (e.g., `To Do` → `In Progress` → `Done`).
   - The Incident Bridge defaults to looking up a transition named `In Progress`. Ensure the issue workflow allows a direct transition from `To Do` / `Open` to `In Progress`. If your project uses custom status labels (e.g., `Investigating`), set `JIRA_IN_PROGRESS_TRANSITION="Investigating"` in the bridge environment configuration.
   - Ensure transitions do not require mandatory fields (e.g., `Resolution`, `Assignee`, or screen popups) which will cause the REST API transition call to fail.

4. **Permission Schemes & API Tokens**:
   - In Jira Free, permission schemes cannot be customized. The API token user must be granted membership in the project (e.g., `Administrator` or `Member` role) so it possesses permissions to browse projects, add comments, transition issues, and attach files.

5. **Attachment File Size & Site Storage**:
   - Jira Free provides a site-wide storage ceiling of 2 GB and a maximum per-attachment file size of 10 MB. The Incident Bridge HTML diagnostic report (`sre_investigation_<KEY>.html`) is lightweight (typically 20 KB–80 KB), well within free limits.

