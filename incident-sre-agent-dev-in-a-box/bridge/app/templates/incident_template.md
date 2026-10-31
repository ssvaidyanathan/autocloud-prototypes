# **{{ date | default("YYYY-MM-DD") }} {{ title | default("Incident Title") }}**

**Date:** {{ date | default("YYYY-MM-DD") }}

**Authors:** {{ author | default("SRE Incident Responder") }}

**Status:** {{ status | default("In progress") }}

**Statuspage:**

- **Reflected On StatusPage?:** {{ statuspage_reflected | default("(if not, include explanation why)") }}
- **Status/Impact Reported?:** {{ statuspage_impact | default("(i.e, Minor, Degraded Performance; Major, Partial Outage; Critical, Major Outage)") }}

**Summary:**
{{ summary | default("- todo") }}

**Impact / Observations (1. customer/externally facing impact/observations; 2. internally facing impact/observations; etc)**
{{ impact | default("- todo") }}

**Impacted Customers (how many customers are impacted, which regions are involved, etc):**
{{ impacted_customers | default("- todo") }}

**Root Causes:**
{{ root_causes | default("- todo") }}

**Trigger:**
{{ trigger | default("- todo") }}

**Detection:**
{{ detection | default("- todo") }}

**Initial Response:**
{{ initial_response | default("- HITL ticket filed via Slack by " ~ (author | default("SRE Incident Responder"))) }}

**Resolution:**
{{ resolution | default("- todo") }}

**Action Items / Jira Tickets:**

| | | |
| --- | --- | --- |
| | | **TODO** |
| | | **TODO** |

**Timeline:**
{{ timeline | default("- " ~ (date | default("YYYY-MM-DD")) ~ " " ~ (time | default("00:00")) ~ " UTC - Incident detected") }}

**Lessons Learned and Incident Feedback:**

- **What went well:** {{ lessons_well | default("todo") }}
- **What went wrong:** {{ lessons_wrong | default("todo") }}
- **What should we do next:** {{ lessons_next | default("todo") }}
- **Where we got lucky:** {{ lessons_lucky | default("todo") }}

**Supporting information**
{{ supporting_info | default("- <link> to alert, Cloud Monitoring, GCP support tickets") }}
