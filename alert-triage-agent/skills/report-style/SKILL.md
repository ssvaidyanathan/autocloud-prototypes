---
name: report-style
description: Standards for generating rich operational 4-bullet executive summaries, ServiceNow work notes in HTML, and clean plain-text incident descriptions.
---

# Report Style & Documentation Skill

## Overview
This skill defines formatting, tone, and structural standards for all alert triage outputs, including ServiceNow incident work notes, ticket descriptions, and standalone HTML diagnostic report attachments.

---

## 1. Dual Formatting Standards for ServiceNow

ServiceNow handles text differently depending on the target field:
- **Work Notes Journal (`work_notes`)**: Does NOT render Markdown. Work notes MUST use `[code]<html>...</html>[/code]` with a valid HTML list (`<ul><li>...</li></ul>`), bold labels (`<b>Problem:</b>`, `<b>Systems:</b>`, `<b>Probable Root Cause:</b>`, `<b>Observations:</b>`, `<b>Recommended Actions:</b>`), and clean formatting. The probable root cause is one line, cut at 600 characters with "... (full text in the attached report)".
- **Incident Description (`description`)**: A plain-text field that displays Markdown formatting literally. Descriptions MUST use clean plain text with labels on their own lines, `- ` or numbered sub-items, and strictly **ZERO markdown asterisks (`*`)** and **ZERO hashes (`#`)**.

---

## 2. ServiceNow Work Notes Standard (`[code]<html>`)

Every executive summary posted into the ServiceNow `work_notes` journal must be wrapped in `[code]` tags using valid HTML:

```html
[code]
<p><b>Executive Summary:</b></p>
<ul>
  <li><b>Problem:</b> Elevated HTTP 5xx error rate (14.2% failure rate) on ata-inventory-api impacting upstream consumers (ata-orders-api, ata-apigee-orders-v1) since 2026-10-23T00:13:00Z (Severity: P1).</li>
  <li><b>Systems:</b> Origin: <code>ata-inventory-api</code> [Cloud Run Service] (91% confidence) | Blast Radius: ata-inventory-api [Cloud Run Service], ata-orders-api [Cloud Run Service], ata-apigee-orders-v1 [Apigee API Proxy] | Dependency Path: Apigee (ata-apigee-orders-v1) -&gt; Cloud Run (ata-orders-api) -&gt; Cloud Run (ata-inventory-api) -&gt; Cloud SQL (ata-inventory-db).</li>
  <li><b>Probable Root Cause:</b> Service degradation on <code>ata-inventory-api</code> (HTTP 5xx error rate 14.2%) after revision ata-inventory-api-00004-abc.</li>
  <li><b>Observations:</b> Cloud Monitoring policy 'ata-inventory-5xx' fired at 2026-10-23T00:13:00Z; HTTP 5xx errors reached 14.2% of total traffic; Container restarts recorded: 2 instances; Log signature: 'Database query timeout after 5000ms'; Revision ata-inventory-api-00004-abc deployed 18m prior to alert trigger.</li>
  <li><b>Recommended Actions:</b> 1. gcloud run services update-traffic ata-inventory-api --to-revisions=ata-inventory-api-00003-prev=100 --region=us-west1 (Verification: Confirm HTTP 5xx error rate drops below 1% in Cloud Monitoring); 2. gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="ata-inventory-api"' --limit=20 (Verification: Confirm healthy container startup); Rollback: Re-route traffic via gcloud run services update-traffic ata-inventory-api --to-latest --region=us-west1 if regression persists.</li>
</ul>
[/code]
```

---

## 3. ServiceNow Description Standard (Clean Plain Text)

The ticket `description` must follow a structured layout with incident metadata preceding the executive summary. It must strictly contain **no markdown asterisks (`*`)** and **no hashes (`#`)**:

```text
Incident Group ID: grp_20261023_inv_001
Priority: P1 (Rationale: Critical threshold breach on core service path)
Origin: ata-inventory-api
Blast Radius: ata-inventory-api, ata-orders-api, ata-apigee-orders-v1
Issue Type: change_induced
Change-Induced: Yes (revision ata-inventory-api-00004-abc deployed 18m ago)
First Seen / Last Alert (UTC): 2026-10-23T00:13:00Z / 2026-10-23T00:19:00Z
Alert List:
  - ata-inventory-5xx | ata-inventory-api | alert-001 | open | 2026-10-23T00:13:00Z | https://console.cloud.google.com/monitoring/alerting/incidents/alert-001?project=cloud-assist-fde-2
GCP Project: cloud-assist-fde-2
Incident File: gs://cloud-assist-fde-2-ata-incidents/knowledge/incident-manager/incident-ata-inventory-5xx-ata-inventory-api-20261023t001300z.md

----------------------------------------------------------------------
Executive Summary:

Problem:
  - Elevated HTTP 5xx error rate (14.2% failure rate) on ata-inventory-api impacting upstream consumers (ata-orders-api, ata-apigee-orders-v1) since 2026-10-23T00:13:00Z (Severity: P1)
Systems:
  - Origin: ata-inventory-api [Cloud Run Service] (91% confidence)
  - Blast Radius: ata-inventory-api [Cloud Run Service], ata-orders-api [Cloud Run Service], ata-apigee-orders-v1 [Apigee API Proxy]
  - Dependency Path: Apigee (ata-apigee-orders-v1) -> Cloud Run (ata-orders-api) -> Cloud Run (ata-inventory-api) -> Cloud SQL (ata-inventory-db)
Observations:
  - Cloud Monitoring policy 'ata-inventory-5xx' triggered at 2026-10-23T00:13:00Z
  - HTTP 5xx errors reached 14.2% of total traffic on ata-inventory-api
  - Log signature: Database query timeout after 5000ms
  - Revision ata-inventory-api-00004-abc deployed 18m ago by ci-deployer
Recommended Actions:
  1. gcloud run services update-traffic ata-inventory-api --to-revisions=ata-inventory-api-00003-prev=100 --region=us-west1 (Verification: Confirm HTTP 5xx error rate drops below 1% in Cloud Monitoring)
  2. gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="ata-inventory-api"' --limit=20 (Verification: Confirm healthy container startup)
  - Rollback: Re-route traffic via gcloud run services update-traffic ata-inventory-api --to-latest --region=us-west1 if regression persists.
----------------------------------------------------------------------
Full diagnostic report attached as HTML (ata-diagnostic-report-grp_20261023_inv_001.html).
```

---

## 4. Rich Operational Criteria for 4-Bullet Summaries

Every synthesized summary must provide rich, concrete, operational details across all 4 bullets:

### Bullet 1: Problem
- **What is failing:** Exact failure mode (HTTP 5xx error rate, database connection exhaustion, SpikeArrest rate limiting, latency spike).
- **For whom:** Upstream callers, consumers, or API proxies directly impacted.
- **Since when:** Exact UTC timestamp of first anomaly or alert onset.
- **Metrics:** Quantitative metric values (error rate %, latency in ms/seconds, connection saturation %).

### Bullet 2: Systems
- **Canonical origin:** Identifier + Resource type (e.g. `ata-inventory-db` [Cloud SQL PostgreSQL Instance], `ata-inventory-api` [Cloud Run Service]).
- **Blast radius:** Every affected service, proxy, and database with its canonical identifier and resource type.
- **Topological dependency path:** Full end-to-end caller-to-callee path (e.g. `Apigee (ata-apigee-orders-v1) -> Cloud Run (ata-orders-api) -> Cloud Run (ata-inventory-api) -> Cloud SQL (ata-inventory-db)`).

### Bullet 3: Observations
- **3 to 5 concrete items** combining numbers and timestamps:
  - Quantitative metric values (connection counts, error rate %, CPU %).
  - Exact log signatures or excerpted error messages.
  - Recent revision deployments with revision IDs, deploy timestamps, or confirmation of no change.
  - Cloud Monitoring alert policies triggered with state and timestamps.

### Bullet 4: Recommended Actions
- **Ordered executable commands:** SRE-ready CLI commands (e.g. `gcloud run services update-traffic ...`, `gcloud sql instances patch ...`).
- **Verification step:** Explicit check to run after each command to verify health recovery.
- **Rollback note:** Safety instruction for rolling back if the action does not resolve the incident.
- **Advisory only:** Agent recommends steps for operator execution; does not execute mutating actions autonomously.

---

## 5. Standalone HTML Diagnostic Report Standards

When an incident group stabilizes, an HTML diagnostic report is rendered via Jinja2 template (`app/templates/report.html`) and attached to the ServiceNow ticket.
- **Self-Contained:** Inline CSS styling, zero external CDN dependencies or web fonts.
- **Durable GCS Archiving:** Persisted to `gs://<project>-ata-incidents/reports/<group_id>.html`.
- **Fallback Attachment:** If attachment upload fails, ticket description is preserved and a work note with the GCS link is appended.
