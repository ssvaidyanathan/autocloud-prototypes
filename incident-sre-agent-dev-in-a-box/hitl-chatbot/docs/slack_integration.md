# Slack Integration Guide: SRE Incident Bot (`hitl-chatbot`)

This guide provides end-to-end instructions for creating, configuring, and connecting the **SRE Incident Bot** Slack application to the deployed `hitl-chatbot` service in Google Cloud Run.

---

## 1. Overview

The **SRE Incident Bot** connects Slack engineers directly to autonomous SRE agents and Human-In-The-Loop (HITL) approval gates.

### Core Capabilities
1. **Use Case A — Deployment Rollout Queries:**  
   Query recent cluster and workload deployments directly from Slack (e.g. `@SRE Incident Bot what changed in the latest rollout?`). The bot calls the Model Context Protocol (`mcp-deploy-context-stub`) service to retrieve recent deployment events, Helm revisions, and rollout summaries.
2. **Use Case B — Alert Triage & HITL Jira Ticket Filing:**  
   Investigate Cloud Monitoring alerts and generate structured Jira incident ticket drafts (e.g. `@SRE Incident Bot file a JIRA ticket in response to the latest sample-ip-healthcheck alert in your-project-id project`).

### Requester-Only Authorization Gate
Mutating operations (such as creating Jira tickets) require explicit human approval via interactive Block Kit buttons:
- **`[Approve]`**, **`[Edit]`**, and **`[Cancel]`** action cards are rendered in-thread.
- **Cryptographic User Binding:** The approval token binds the requesting user's Slack ID (`requester_id`).
- **Authorization Enforcement:** If another team member clicks `[Approve]`, the request is rejected with `HTTP 403 Forbidden` (`Approval rejected: user '...' is not authorized to approve draft created by '...'`), and the single-use token remains valid so the original requester can approve.
- **RCA Delivery:** Once approved and investigated by the SRE agent via the Dev-In-A-Box (`dib-*`) bridge, Root-Cause Analysis (RCA) findings are automatically posted back to the original Slack thread.

**Estimated Setup Time:** ~10 minutes.

---

## 2. Prerequisites

Before beginning, ensure you have:
1. **Slack Workspace Permissions:** Access to install applications in [your-workspace.slack.com](https://your-workspace.slack.com).
2. **Target Slack Channel:** The `#sre-alerts` channel in `your-workspace`.
3. **Google Cloud Access:** Permissions to add Secret Manager versions in GCP project `your-project-id` (either via `gcloud` CLI or the Google Cloud Console).
4. **Deployed Chatbot Service:** Active Cloud Run deployment `hitl-chatbot` in region `us-central1`.

### Fixed Facts & Endpoints
| Parameter | Value |
|---|---|
| **Slack Workspace** | `your-workspace.slack.com` |
| **Slack Channel** | `#sre-alerts` |
| **Request URL (Events & Interactivity)** | `https://hitl-chatbot-sample-uc.a.run.app/slack/events` |
| **GCP Project** | `your-project-id` |
| **GCP Region** | `us-central1` |
| **Secret Manager Secret (Token)** | `hitl-slack-bot-token` |
| **Secret Manager Secret (Signing Secret)** | `hitl-slack-signing-secret` |

---

## 3. Option A (Recommended): Create App from Manifest

Using a Slack App Manifest automates scope selection, event subscriptions, and interactivity settings in a single copy-paste operation.

### Step 1: Open the Slack App Creator
1. Navigate to [https://api.slack.com/apps](https://api.slack.com/apps).
2. Click **Create New App**.
3. In the popup dialog, select **From an app manifest**.
4. Select the target workspace: **your-workspace**.
5. Click **Next**.

### Step 2: Paste the YAML Manifest
Select the **YAML** tab and paste the following manifest specification:

```yaml
display_information:
  name: SRE Incident Bot
  description: Autonomous SRE incident triage, deployment context inspection, and HITL approval bot.
  background_color: "#1d2129"
features:
  app_home:
    home_tab_enabled: false
    messages_tab_enabled: true
    messages_tab_read_only_enabled: false
  bot_user:
    display_name: SRE Incident Bot
    always_online: true
oauth_config:
  scopes:
    bot:
      - app_mentions:read
      - chat:write
      - im:history
      - im:read
      - im:write
      - channels:history
settings:
  event_subscriptions:
    request_url: https://hitl-chatbot-sample-uc.a.run.app/slack/events
    bot_events:
      - app_mention
      - message.im
  interactivity:
    is_enabled: true
    request_url: https://hitl-chatbot-sample-uc.a.run.app/slack/events
  org_deploy_enabled: false
  socket_mode_enabled: false
  token_rotation_enabled: false
```

### Step 3: Review and Create
1. Click **Next**.
2. Review the summarized features and bot scopes:
   - `app_mentions:read` (Receive `@SRE Incident Bot` mentions in channels)
   - `chat:write` (Post replies and approval action cards)
   - `im:history`, `im:read`, `im:write` (Direct message communication)
   - `channels:history` (Read message thread context in public channels)
   - Event Subscriptions: `app_mention`, `message.im`
   - Interactivity: Enabled
3. Click **Create**. You will be redirected to the app's **Basic Information** page.

> **Note on Initial URL Verification:**  
> When the manifest is submitted, Slack will attempt to verify the Request URL. Because the chatbot returns `HTTP 503` until credentials are populated into Secret Manager and a new revision is rolled, Slack may show a warning: *"Your URL didn't respond with the value of the challenge parameter."* You can proceed with app creation and verify the URL after completing Step 6 and Step 8.

---

## 4. Option B: Manual Setup (Click-by-Click)

If you prefer configuring each Slack feature manually instead of using the manifest:

### 1. Basic Information
1. Open [https://api.slack.com/apps](https://api.slack.com/apps) and click **Create New App** -> **From scratch**.
2. App Name: `SRE Incident Bot`.
3. Workspace: Pick `your-workspace`.
4. Click **Create App**.

### 2. OAuth & Permissions Scopes
1. In the left navigation sidebar under **Features**, click **OAuth & Permissions**.
2. Scroll down to **Scopes** -> **Bot Token Scopes**.
3. Click **Add an OAuth Scope** and add each of the following scopes:
   - `app_mentions:read`
   - `chat:write`
   - `im:history`
   - `im:read`
   - `im:write`
   - `channels:history`
4. Leave **User Token Scopes** empty.

### 3. App Home & Direct Messaging
1. In the left sidebar under **Features**, select **App Home**.
2. Under **Show Tabs**, toggle **Messages Tab** to **ON**.
3. Check **Allow users to send Slash commands and messages from the messages tab**.

### 4. Interactivity & Shortcuts
1. In the left sidebar under **Features**, click **Interactivity & Shortcuts**.
2. Toggle the switch to **ON**.
3. Set **Request URL** to:
   ```text
   https://hitl-chatbot-sample-uc.a.run.app/slack/events
   ```
4. Click **Save Changes**. *(If URL verification fails with 503, save after Step 8).*

### 5. Event Subscriptions
1. In the left sidebar under **Features**, click **Event Subscriptions**.
2. Toggle **Enable Events** to **ON**.
3. Set **Request URL** to:
   ```text
   https://hitl-chatbot-sample-uc.a.run.app/slack/events
   ```
4. Under **Subscribe to bot events**, click **Add Bot User Event** and add:
   - `app_mention`
   - `message.im`
5. Click **Save Changes**.

---

## 5. Install App & Copy Credentials

1. In the left sidebar under **Settings**, click **Install App** (or return to **OAuth & Permissions**).
2. Click **Install to Workspace** (or **Reinstall to Workspace**).
3. Review the permissions requested by `SRE Incident Bot` and click **Allow**.
4. Copy the **Bot User OAuth Token**:
   - Location: **OAuth & Permissions** -> **OAuth Tokens for Your Workspace**.
   - Format: Starts with `xoxb-...`.
   - Purpose: Authenticates API calls made by the bot (`SLACK_BOT_TOKEN`).
5. Copy the **Signing Secret**:
   - Location: In the left sidebar, click **Basic Information** -> scroll to **App Credentials**.
   - Click **Show** next to **Signing Secret**.
   - Format: 32-character hexadecimal string.
   - Purpose: Verifies authenticity of Slack webhooks (`SLACK_SIGNING_SECRET`).

---

## 6. Store Secrets in Google Cloud Secret Manager

Populate the pre-created secrets in GCP project `your-project-id`. To protect credentials from shell history leaks, use `read -s` to ingest the secrets from standard input.

### Option 6A: Terminal via `gcloud` (Recommended)

Run the following commands in your shell:

```bash
# Set GCP Project
PROJECT_ID="your-project-id"

# 1. Store Bot User OAuth Token (xoxb-...)
read -s -p "Paste Slack Bot User OAuth Token (xoxb-...): " SLACK_BOT_TOKEN; echo
printf '%s' "$SLACK_BOT_TOKEN" | gcloud secrets versions add hitl-slack-bot-token \
  --data-file=- \
  --project="${PROJECT_ID}"
unset SLACK_BOT_TOKEN

# 2. Store Slack Signing Secret
read -s -p "Paste Slack Signing Secret: " SLACK_SIGNING_SECRET; echo
printf '%s' "$SLACK_SIGNING_SECRET" | gcloud secrets versions add hitl-slack-signing-secret \
  --data-file=- \
  --project="${PROJECT_ID}"
unset SLACK_SIGNING_SECRET
```

Confirm that the new secret versions were created:
```bash
gcloud secrets versions list hitl-slack-bot-token --project=your-project-id
gcloud secrets versions list hitl-slack-signing-secret --project=your-project-id
```

### Option 6B: Google Cloud Console UI

If you do not have the `gcloud` CLI configured:
1. Open the [Google Cloud Secret Manager Console](https://console.cloud.google.com/security/secret-manager?project=your-project-id).
2. Click secret **`hitl-slack-bot-token`**.
3. Click **Add Version** (or **Actions** -> **Add version**).
4. Paste the `xoxb-...` token into the **Secret value** field and click **Add Version**.
5. Return to Secret Manager, click secret **`hitl-slack-signing-secret`**.
6. Click **Add Version**, paste the signing secret, and click **Add Version**.

---

## 7. Invite Bot to the Channel

Add the bot to the designated operations channel so it can receive mentions:

1. Open Slack and navigate to workspace **your-workspace**.
2. Open channel **#sre-alerts**.
3. Run the slash command:
   ```text
   /invite @SRE Incident Bot
   ```
4. Verify that the bot is added to `#sre-alerts`.

---

## 8. Notify Coordinator & Complete URL Verification

Because Cloud Run services read Secret Manager secrets at container startup, the service requires a new Cloud Run revision to load the newly added secrets.

1. **Notify Coordinator:** Send a message to the coordinator (`sre-platform-coord`) saying:
   > *"Slack secrets hitl-slack-bot-token and hitl-slack-signing-secret are populated. Done."*
2. **Coordinator Action:** The coordinator deploys a new Cloud Run revision that mounts the new secret versions.
3. **Verify Slack URL Subscriptions:**
   - Once the new revision is live, return to [https://api.slack.com/apps](https://api.slack.com/apps) -> **SRE Incident Bot**.
   - Open **Event Subscriptions**.
   - If the Request URL previously showed an error, click **Retry** or re-enter:
     `https://hitl-chatbot-sample-uc.a.run.app/slack/events`
   - Slack will send the URL verification challenge. You will see a green checkmark: **Verified**.
   - Open **Interactivity & Shortcuts**, confirm the same URL is verified, and click **Save Changes**.

---

## 9. Testing & End-to-End Verification

Conduct end-to-end verification in `#sre-alerts`.

### Test 1: Deployment Rollout Inspection (Use Case A)

In `#sre-alerts`, post:
```text
@SRE Incident Bot what changed in the latest rollout?
```

**Expected Bot Behavior:**
- The bot replies in a thread.
- Output lists recent deployment events queried from `mcp-deploy-context-stub`:
  - Workload and Helm release revisions (e.g. Revision 89, tool `helm`).
  - Deploy Context Note explaining the deployment context and Kubernetes Agent metadata.

---

### Test 2: Alert Triage & Jira Draft Creation (Use Case B)

In `#sre-alerts`, post:
```text
@SRE Incident Bot file a JIRA ticket in response to the latest sample-ip-healthcheck alert in your-project-id project
```

**Expected Bot Behavior:**
1. **Interactive Preview:** The bot resolves the alert policy (`Alert Policy for sample-ip-healthcheck-3`) and responds in-thread with a Block Kit preview:
   - Summary and Violation ID.
   - Requester identification binding your Slack user ID.
   - Interactive buttons: **`[Approve]`**, **`[Edit]`**, **`[Cancel]`**.
2. **Requester-Only Security Check:**
   - If a **different user** clicks `[Approve]`, Slack displays an ephemeral rejection message:
     *"Approval rejected: user '...' is not authorized to approve draft created by '...'"*. The ticket is not created, and the token remains intact.
   - When the **original requester** clicks `[Approve]`:
     - The token is cryptographically verified and consumed.
     - The bot calls the bridge API to create the Jira issue.
     - Success confirmation is posted: `✅ Jira Issue Successfully Created: AMJTEST-XX`.
3. **Automated Root-Cause Analysis (RCA) Post-Back:**
   - The Jira ticket transition triggers the `dib-incident-bridge` and `dib-sre-agent` Cloud Tasks investigation.
   - Upon investigation completion, the bridge invokes `/internal/rca-callback`.
   - The SRE bot posts the final RCA report back into the Slack incident thread (summarizing pod rescheduling delays, replica metrics, and resolution timeline).

> [!IMPORTANT]
> **Jira Automation Rule Prerequisite:**  
> Autonomous investigation and RCA post-back require the Jira Cloud Automation rules configured in Atlassian Jira (`https://your-domain.atlassian.net`):
> - **Existing Rule:** Trigger condition `labels does not contain "hitl"` routes to `isa-incident-bridge`.
> - **DIB HITL Rule:** Trigger condition `labels contains "hitl"` routes to `https://dib-incident-bridge-sample-uc.a.run.app/webhook/jira` with header `X-Webhook-Token: <dib-bridge-jira-webhook-token>`.

---

## 10. Troubleshooting

| Symptom | Root Cause | Solution |
|---|---|---|
| **Slack Request URL verification fails with HTTP 503** | Secrets not loaded yet or container running earlier revision. | The chatbot returns 503 until secrets are mounted. Confirm secrets are populated in Secret Manager (Step 6) and ask coordinator to roll a new revision (Step 8), then re-verify in Slack. |
| **`invalid_signature` or `dispatch_failed`** | `hitl-slack-signing-secret` value in Secret Manager does not match the signing secret in Slack Basic Information. | Verify the Signing Secret in Slack **Basic Information** -> **App Credentials**. Re-upload version to `hitl-slack-signing-secret` using `read -s`. Ask coordinator to roll a new revision. |
| **Bot does not respond to `@SRE Incident Bot` in channel** | Bot was not invited to channel, or `app_mentions:read` scope / `app_mention` event is missing. | Run `/invite @SRE Incident Bot` in `#sre-alerts`. Verify under **Event Subscriptions** that `app_mention` is added. If scopes were changed, click **Reinstall to Workspace**. |
| **Direct messages return "Messaging disabled"** | Messages Tab is disabled in App Home. | Go to **App Home** -> **Show Tabs** -> Enable **Messages Tab** and check **Allow users to send Slash commands and messages from the messages tab**. |
| **Clicking `[Approve]` does nothing or shows a warning triangle** | Interactivity is disabled or Request URL is incorrect. | Go to **Interactivity & Shortcuts** -> ensure toggle is **ON** and Request URL is set to `https://hitl-chatbot-sample-uc.a.run.app/slack/events`. Click **Save Changes**. |
| **Clicking `[Approve]` returns "Approval rejected: user not authorized"** | You are not the user who requested the ticket draft. | HITL security invariant: only the user who initiated the prompt can approve. The requesting user must click `[Approve]`. |
| **Scope changes in Slack do not take effect** | App was not reinstalled after updating permissions. | After adding or modifying OAuth scopes or events, navigate to **OAuth & Permissions** and click **Reinstall to Workspace**. |
