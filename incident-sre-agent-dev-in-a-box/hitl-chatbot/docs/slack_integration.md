# Slack Integration: SRE Incident Bot (`hitl-chatbot`)

Setup guide for connecting SRE Incident Bot to `hitl-chatbot` on Cloud Run.

---

## 1. Overview

Connects Slack channels to SRE investigation agents with HITL approval gates.

### Capabilities
- **Rollout queries (Use Case A):** `@SRE Incident Bot what changed in the latest rollout?` queries `mcp-deploy-context-stub` for recent deployment events and revisions.
- **Alert triage (Use Case B):** `@SRE Incident Bot file a JIRA ticket in response to the latest sample-ip-healthcheck alert in your-project-id project` queries Cloud Monitoring and drafts a Jira ticket.

### Approval Gate
- Actions render `[Approve]`, `[Edit]`, `[Cancel]` buttons in-thread.
- Token binds requester Slack ID (`requester_id`).
- Non-requester clicks receive HTTP 403; token remains valid for requester.
- On approval, bridge runs investigation and posts RCA findings back to thread.

---

## 2. Prerequisites

- Admin access to `your-workspace.slack.com`.
- Access to `#sre-alerts`.
- Secret Manager write access in GCP project `your-project-id`.
- Deployed `hitl-chatbot` service in `us-central1`.

| Parameter | Value |
| --- | --- |
| Slack Workspace | `your-workspace.slack.com` |
| Slack Channel | `#sre-alerts` |
| Request URL | `https://hitl-chatbot-sample-uc.a.run.app/slack/events` |
| GCP Project | `your-project-id` |
| GCP Region | `us-central1` |
| Secret: Bot Token | `hitl-slack-bot-token` |
| Secret: Signing Secret | `hitl-slack-signing-secret` |

---

## 3. Option A: Create App from Manifest (Recommended)

1. Open [api.slack.com/apps](https://api.slack.com/apps) -> **Create New App** -> **From an app manifest**.
2. Select **your-workspace** -> **Next**.
3. Select **YAML** and paste:

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

4. Click **Next** -> **Create**.

> **Note:** Initial URL verification may warn with 503 until secrets are loaded in Step 6 and 8. Proceed with app creation.

---

## 4. Option B: Manual Setup

1. **Create App:** [api.slack.com/apps](https://api.slack.com/apps) -> **Create New App** -> **From scratch**. Name: `SRE Incident Bot`. Workspace: `your-workspace`.
2. **Scopes:** Under **OAuth & Permissions** -> **Bot Token Scopes**, add:
   `app_mentions:read`, `chat:write`, `im:history`, `im:read`, `im:write`, `channels:history`.
3. **App Home:** Under **App Home** -> enable **Messages Tab** and check **Allow users to send Slash commands and messages from the messages tab**.
4. **Interactivity:** Under **Interactivity & Shortcuts** -> toggle **ON**. Set Request URL to `https://hitl-chatbot-sample-uc.a.run.app/slack/events`. Save.
5. **Events:** Under **Event Subscriptions** -> toggle **ON**. Set Request URL to `https://hitl-chatbot-sample-uc.a.run.app/slack/events`. Subscribe to `app_mention` and `message.im`. Save.

---

## 5. Install App & Copy Credentials

1. Go to **Install App** -> click **Install to Workspace** -> **Allow**.
2. Copy credentials:
   - **Bot User OAuth Token (`xoxb-...`):** Under **OAuth & Permissions**. Maps to `SLACK_BOT_TOKEN`.
   - **Signing Secret:** Under **Basic Information** -> **App Credentials**. Maps to `SLACK_SIGNING_SECRET`.

---

## 6. Store Secrets in Secret Manager

### Terminal (`gcloud`)

```bash
PROJECT_ID="your-project-id"

read -s -p "Paste Slack Bot Token (xoxb-...): " SLACK_BOT_TOKEN; echo
printf '%s' "$SLACK_BOT_TOKEN" | gcloud secrets versions add hitl-slack-bot-token \
  --data-file=- --project="${PROJECT_ID}"
unset SLACK_BOT_TOKEN

read -s -p "Paste Slack Signing Secret: " SLACK_SIGNING_SECRET; echo
printf '%s' "$SLACK_SIGNING_SECRET" | gcloud secrets versions add hitl-slack-signing-secret \
  --data-file=- --project="${PROJECT_ID}"
unset SLACK_SIGNING_SECRET
```

Verify:
```bash
gcloud secrets versions list hitl-slack-bot-token --project=your-project-id
gcloud secrets versions list hitl-slack-signing-secret --project=your-project-id
```

### Cloud Console

1. Open [Secret Manager](https://console.cloud.google.com/security/secret-manager?project=your-project-id).
2. Add version to `hitl-slack-bot-token` with the `xoxb-...` value.
3. Add version to `hitl-slack-signing-secret` with the signing secret.

---

## 7. Invite Bot to Channel

In `#sre-alerts`, run:
```text
/invite @SRE Incident Bot
```

---

## 8. Roll Revision & Verify URL

1. Notify coordinator (`sre-platform-coord`):
   > "Slack secrets hitl-slack-bot-token and hitl-slack-signing-secret are populated. Done."
2. Coordinator deploys new Cloud Run revision mounting the secret versions.
3. In [api.slack.com/apps](https://api.slack.com/apps) -> **SRE Incident Bot**:
   - Under **Event Subscriptions**, verify Request URL: `https://hitl-chatbot-sample-uc.a.run.app/slack/events` shows **Verified**.
   - Under **Interactivity & Shortcuts**, verify same URL and save.

---

## 9. Verification

### Test 1: Deployment Rollout Inspection (Use Case A)

In `#sre-alerts`:
```text
@SRE Incident Bot what changed in the latest rollout?
```

**Expected:** Bot replies in thread with revisions and notes from `mcp-deploy-context-stub`.

### Test 2: Alert Triage & Jira Draft (Use Case B)

In `#sre-alerts`:
```text
@SRE Incident Bot file a JIRA ticket in response to the latest sample-ip-healthcheck alert in your-project-id project
```

**Expected:**
1. Bot resolves alert and posts Block Kit card with `[Approve]`, `[Edit]`, `[Cancel]`.
2. Non-requester click returns ephemeral rejection: `"Approval rejected: user '...' is not authorized to approve draft created by '...'"`
3. Requester click creates Jira issue: `✅ Jira Issue Successfully Created: AMJTEST-XX`.
4. Bridge triggers investigation and posts RCA report to thread upon completion.

> [!IMPORTANT]
> **Jira Automation Rule:**
> - Non-HITL: `labels does not contain "hitl"` -> `isa-incident-bridge`.
> - HITL: `labels contains "hitl"` -> `https://dib-incident-bridge-sample-uc.a.run.app/webhook/jira` with `X-Webhook-Token: <dib-bridge-jira-webhook-token>`.

---

## 10. Troubleshooting

| Symptom | Root Cause | Solution |
| --- | --- | --- |
| **Request URL verification fails with HTTP 503** | Secrets not loaded or old revision running. | Populate secrets (Step 6) and ask coordinator to roll revision (Step 8). Retry URL verification. |
| **`invalid_signature` or `dispatch_failed`** | `hitl-slack-signing-secret` does not match Slack App Credentials. | Re-upload signing secret from Slack Basic Information to Secret Manager. Roll new revision. |
| **Bot does not respond in channel** | Bot not in channel or missing `app_mention` scope/event. | Run `/invite @SRE Incident Bot` in `#sre-alerts`. Verify `app_mention` event. Reinstall app if scopes changed. |
| **Direct messages return "Messaging disabled"** | Messages Tab disabled. | Enable Messages Tab under **App Home**. |
| **`[Approve]` click shows warning triangle** | Interactivity disabled or URL wrong. | Enable Interactivity and set Request URL to `/slack/events`. Save changes. |
| **`[Approve]` returns "user not authorized"** | Clicker is not draft creator. | Only initiating user can approve. Initiating user must click `[Approve]`. |
| **Scope changes do not take effect** | App not reinstalled. | Click **Reinstall to Workspace** under **OAuth & Permissions**. |
