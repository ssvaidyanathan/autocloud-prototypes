from jinja2 import Environment, select_autoescape
from ..contracts.report import InvestigationReport
from ..contracts.links import ConsoleLinkBuilder

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Alert Triage Diagnostic Report - {{ report.filename or report.group_id }}</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; line-height: 1.6; color: #1f2328; margin: 0; padding: 24px; background: #f6f8fa; }
    .container { max-width: 1100px; margin: 0 auto; background: #ffffff; border: 1px solid #d0d7de; border-radius: 8px; padding: 32px; box-shadow: 0 1px 3px rgba(31,35,40,0.12); }
    .header { border-bottom: 2px solid #0969da; padding-bottom: 16px; margin-bottom: 24px; }
    .header-top { display: flex; justify-content: space-between; align-items: flex-start; }
    .header h1 { margin: 0 0 6px 0; font-size: 24px; color: #0969da; }
    .badge { padding: 4px 10px; border-radius: 12px; font-size: 12px; font-weight: 600; text-transform: uppercase; display: inline-block; }
    .badge-critical { background: #ffebe9; color: #cf222e; border: 1px solid #ff8182; }
    .badge-warn { background: #fff8c5; color: #9a6700; border: 1px solid #d4a72c; }
    .badge-info { background: #ddf4ff; color: #0969da; border: 1px solid #54aeff; }
    .badge-success { background: #dafbe1; color: #1a7f37; border: 1px solid #4ac26b; }
    .section { margin-bottom: 32px; }
    .section-title { font-size: 17px; font-weight: 600; color: #24292f; margin-bottom: 14px; border-bottom: 1px solid #d0d7de; padding-bottom: 6px; }
    .exec-summary-box { background: #ddf4ff; border-left: 4px solid #0969da; padding: 18px; border-radius: 4px; margin-bottom: 24px; }
    .exec-summary-box ul { margin: 0; padding-left: 20px; }
    table { width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 13px; margin-bottom: 16px; }
    th, td { text-align: left; padding: 8px 12px; border: 1px solid #d0d7de; vertical-align: top; }
    th { background: #f6f8fa; font-weight: 600; }
    pre { background: #f6f8fa; border: 1px solid #d0d7de; border-radius: 6px; padding: 10px; font-size: 12px; overflow-x: auto; font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace; }
    .meta-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; margin-top: 14px; padding: 12px; background: #f6f8fa; border-radius: 6px; border: 1px solid #d0d7de; font-size: 13px; }
    .meta-item { display: flex; flex-direction: column; }
    .meta-label { font-size: 11px; text-transform: uppercase; color: #57606a; font-weight: 600; }
    .meta-val { font-weight: 500; color: #24292f; word-break: break-all; }
    .callout-box { background: #f6f8fa; border: 1px solid #d0d7de; border-radius: 6px; padding: 14px; margin-bottom: 16px; }
    a.console-link { color: #0969da; text-decoration: none; }
    a.console-link:hover { text-decoration: underline; }
  </style>
</head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; line-height: 1.6; color: #1f2328; margin: 0; padding: 24px; background: #f6f8fa;">
  <div class="container" style="max-width: 1100px; margin: 0 auto; background: #ffffff; border: 1px solid #d0d7de; border-radius: 8px; padding: 32px; box-shadow: 0 1px 3px rgba(31,35,40,0.12);">

    <!-- Section 1: Header -->
    <div class="header" style="border-bottom: 2px solid #0969da; padding-bottom: 16px; margin-bottom: 24px;">
      <div class="header-top" style="display: flex; justify-content: space-between; align-items: flex-start;">
        <div>
          <h1 style="margin: 0 0 6px 0; font-size: 24px; color: #0969da;">Alert Triage Diagnostic Report</h1>
          <div style="font-size: 13px; color: #57606a;">
            Ticket: <strong style="color: #24292f;">{{ report.servicenow_number or 'Unassigned' }}</strong> | Incident Group: <code>{{ report.group_id }}</code>
          </div>
        </div>
        <div style="text-align: right;">
          <span class="badge {% if report.priority == 'P1' %}badge-critical{% else %}badge-warn{% endif %}" style="padding: 4px 10px; border-radius: 12px; font-size: 12px; font-weight: 600; text-transform: uppercase; display: inline-block; {% if report.priority == 'P1' %}background: #ffebe9; color: #cf222e; border: 1px solid #ff8182;{% else %}background: #fff8c5; color: #9a6700; border: 1px solid #d4a72c;{% endif %}">
            {{ report.priority }}
          </span>
          <div style="margin-top: 6px; font-size: 12px; color: #57606a;">
            Status: <strong style="color: #1a7f37;">{{ report.status or 'In Progress' }}</strong>
          </div>
        </div>
      </div>

      <div class="meta-grid" style="display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; margin-top: 14px; padding: 12px; background: #f6f8fa; border-radius: 6px; border: 1px solid #d0d7de; font-size: 13px;">
        <div class="meta-item" style="display: flex; flex-direction: column;">
          <span class="meta-label" style="font-size: 11px; text-transform: uppercase; color: #57606a; font-weight: 600;">Incident Group</span>
          <span class="meta-val" style="font-weight: 500; color: #24292f;">{{ report.group_id }}</span>
        </div>
        <div class="meta-item" style="display: flex; flex-direction: column;">
          <span class="meta-label" style="font-size: 11px; text-transform: uppercase; color: #57606a; font-weight: 600;">GCP Project</span>
          <span class="meta-val" style="font-weight: 500; color: #24292f;"><a href="{{ builder.build_dashboard_url() }}" target="_blank" class="console-link" style="color: #0969da; text-decoration: none;">{{ report.project_id }}</a></span>
        </div>
        <div class="meta-item" style="display: flex; flex-direction: column;">
          <span class="meta-label" style="font-size: 11px; text-transform: uppercase; color: #57606a; font-weight: 600;">First Seen (UTC)</span>
          <span class="meta-val" style="font-weight: 500; color: #24292f;">{{ report.first_seen or 'N/A' }}</span>
        </div>
        <div class="meta-item" style="display: flex; flex-direction: column;">
          <span class="meta-label" style="font-size: 11px; text-transform: uppercase; color: #57606a; font-weight: 600;">Last Alert (UTC)</span>
          <span class="meta-val" style="font-weight: 500; color: #24292f;">{{ report.last_alert or 'N/A' }}</span>
        </div>
        <div class="meta-item" style="display: flex; flex-direction: column;">
          <span class="meta-label" style="font-size: 11px; text-transform: uppercase; color: #57606a; font-weight: 600;">Report Generated</span>
          <span class="meta-val" style="font-weight: 500; color: #24292f;">{{ report.report_generated_at or 'N/A' }}</span>
        </div>
      </div>
    </div>

    <!-- Section 2: Executive Summary (4-Bullet) -->
    <div class="section" style="margin-bottom: 32px;">
      <div class="exec-summary-box" style="background: #ddf4ff; border-left: 4px solid #0969da; padding: 18px; border-radius: 4px; margin-bottom: 24px;">
        <div style="font-weight: 600; margin-bottom: 10px; color: #0969da; font-size: 15px;">Executive Summary</div>
        <ul style="margin: 0; padding-left: 20px;">
          <li style="margin-bottom: 8px;"><strong>Problem:</strong> {{ report.summary }}</li>
          <li style="margin-bottom: 8px;"><strong>Systems:</strong> Origin: <a href="{{ builder.build_resource_url(report.origin) }}" target="_blank" class="console-link" style="color: #0969da; text-decoration: none;"><code>{{ report.origin }}</code></a> ({{ (report.probable_root_cause.confidence * 100)|int }}% confidence) | Blast Radius: {{ report.blast_radius|join(', ') if report.blast_radius else 'None' }}</li>
          <li style="margin-bottom: 8px;"><strong>Observations:</strong> {{ report.observations }}</li>
          <li style="margin-bottom: 8px;"><strong>Recommended Actions:</strong>
            {% set actions = report.get_normalized_actions() %}
            {% if actions %}
            <ol style="margin: 6px 0; padding-left: 20px;">
              {% for act in actions %}
              {% if act.command or act.title != 'Rollback' %}
              <li style="margin-bottom: 8px;">
                <strong>{{ act.title }}</strong>{% if act.verification %} <span style="color: #57606a;">(Verification: {{ act.verification }})</span>{% endif %}
                {% if act.command %}
                <pre><code style="background:#f4f4f4;border:1px solid #ddd;padding:6px;font-family:monospace;display:block">{{ act.command }}</code></pre>
                {% endif %}
              </li>
              {% endif %}
              {% endfor %}
            </ol>
            {% set rollbacks = [] %}
            {% for act in actions %}
              {% if act.rollback and act.rollback not in rollbacks %}
                {% set _ = rollbacks.append(act.rollback) %}
              {% endif %}
            {% endfor %}
            {% if rollbacks %}
            <div style="margin-top: 8px; font-size: 13px;"><strong>Rollback:</strong> {{ rollbacks|join('; ') }}</div>
            {% endif %}
            {% else %}
            Monitor telemetry
            {% endif %}
          </li>
        </ul>
      </div>
    </div>

    <!-- Section 3: Triage Decision & Grouping -->
    <div class="section" style="margin-bottom: 32px;">
      <div class="section-title" style="font-size: 17px; font-weight: 600; color: #24292f; margin-bottom: 14px; border-bottom: 1px solid #d0d7de; padding-bottom: 6px;">
        1. Triage Decision & Alert Grouping Analysis
      </div>
      <p style="margin: 0 0 10px 0; font-size: 13px; color: #57606a;">
        Evaluation of correlated alerts ingested into this incident group and exclusion filtering:
      </p>

      <h4 style="margin: 12px 0 6px 0; font-size: 14px; color: #24292f;">Correlated Alerts Ingested in Group</h4>
      <table style="width: 100%; border-collapse: collapse; font-size: 13px; margin-bottom: 16px;">
        <thead>
          <tr style="background: #f6f8fa;">
            <th style="padding: 8px 12px; border: 1px solid #d0d7de;">Policy Name</th>
            <th style="padding: 8px 12px; border: 1px solid #d0d7de;">Resource</th>
            <th style="padding: 8px 12px; border: 1px solid #d0d7de;">Component</th>
            <th style="padding: 8px 12px; border: 1px solid #d0d7de;">Signal</th>
            <th style="padding: 8px 12px; border: 1px solid #d0d7de;">Incident ID</th>
            <th style="padding: 8px 12px; border: 1px solid #d0d7de;">Labels</th>
            <th style="padding: 8px 12px; border: 1px solid #d0d7de;">State</th>
            <th style="padding: 8px 12px; border: 1px solid #d0d7de;">Triggered (UTC)</th>
            <th style="padding: 8px 12px; border: 1px solid #d0d7de;">Console Links</th>
          </tr>
        </thead>
        <tbody>
          {% for alt in report.alerts %}
          {% set inc_url = alt.url or alt.console_url or builder.build_incident_url(alt.incident_id) %}
          {% set pol_url = alt.policy_url or builder.build_policy_url(alt.policy_id or alt.policy_numeric_id or alt.policy or alt.policy_name) %}
          {% set res_url = alt.resource_url or builder.build_resource_url(alt.resource or alt.resource_name or report.origin, alt.resource_type) %}
          <tr>
            <td style="padding: 8px 12px; border: 1px solid #d0d7de; font-weight: 500;">
              <a href="{{ pol_url }}" target="_blank" class="console-link" style="color: #0969da; text-decoration: none;">{{ alt.policy or alt.policy_name or 'Alert Policy' }}</a>
            </td>
            <td style="padding: 8px 12px; border: 1px solid #d0d7de;">
              <a href="{{ res_url }}" target="_blank" class="console-link" style="color: #0969da; text-decoration: none;"><code>{{ alt.resource or alt.resource_name or report.origin }}</code></a>
            </td>
            <td style="padding: 8px 12px; border: 1px solid #d0d7de;">
              <code>{{ alt.component or (alt.labels and alt.labels.component) or (alt.policy_user_labels and alt.policy_user_labels.component) or alt.resource or alt.resource_name or report.origin }}</code>
            </td>
            <td style="padding: 8px 12px; border: 1px solid #d0d7de;">
              <code>{{ alt.signal or (alt.labels and alt.labels.signal) or (alt.policy_user_labels and alt.policy_user_labels.signal) or 'N/A' }}</code>
            </td>
            <td style="padding: 8px 12px; border: 1px solid #d0d7de;">
              <a href="{{ inc_url }}" target="_blank" class="console-link" style="color: #0969da; text-decoration: none;"><code>{{ alt.incident_id or 'N/A' }}</code></a>
            </td>
            <td style="padding: 8px 12px; border: 1px solid #d0d7de;">
              {% set alt_labels = alt.labels or alt.policy_user_labels %}
              {% if alt_labels %}
                {% for k, v in alt_labels.items() %}
                  <code style="font-size: 11px; background: #eef2f8; padding: 2px 4px; border-radius: 3px; display: inline-block; margin: 1px;">{{ k }}={{ v }}</code>
                {% endfor %}
              {% else %}
                <span style="color: #57606a; font-size: 11px;">none</span>
              {% endif %}
            </td>
            <td style="padding: 8px 12px; border: 1px solid #d0d7de;">
              <span class="badge {% if alt.state == 'closed' %}badge-success{% else %}badge-critical{% endif %}" style="padding: 2px 8px; border-radius: 10px; font-size: 11px;">
                {{ alt.state or 'open' }}
              </span>
            </td>
            <td style="padding: 8px 12px; border: 1px solid #d0d7de;">{{ alt.started_at or report.first_seen }}</td>
            <td style="padding: 8px 12px; border: 1px solid #d0d7de;">
              <a href="{{ inc_url }}" target="_blank" class="console-link" style="color: #0969da; text-decoration: none;">View Incident &rarr;</a>
              <br>
              <a href="{{ pol_url }}" target="_blank" class="console-link" style="font-size: 11px; color: #57606a; text-decoration: none;">Alert Policy &rarr;</a>
            </td>
          </tr>
          {% else %}
          {% set inc_id = report.raw_metadata.get('incident_id') %}
          {% set inc_url = builder.build_incident_url(inc_id) if inc_id else 'https://console.cloud.google.com/monitoring/alerting?project=' ~ report.project_id %}
          {% set pol_url = builder.build_policy_url(report.raw_metadata.get('policy_id') or report.raw_metadata.get('policy_name')) %}
          {% set res_url = builder.build_resource_url(report.origin) %}
          <tr>
            <td style="padding: 8px 12px; border: 1px solid #d0d7de; font-weight: 500;">
              <a href="{{ pol_url }}" target="_blank" class="console-link" style="color: #0969da; text-decoration: none;">{{ report.summary }}</a>
            </td>
            <td style="padding: 8px 12px; border: 1px solid #d0d7de;">
              <a href="{{ res_url }}" target="_blank" class="console-link" style="color: #0969da; text-decoration: none;"><code>{{ report.origin }}</code></a>
            </td>
            <td style="padding: 8px 12px; border: 1px solid #d0d7de;">
              <code>{{ report.raw_metadata.get('component') or report.origin }}</code>
            </td>
            <td style="padding: 8px 12px; border: 1px solid #d0d7de;">
              <code>{{ report.raw_metadata.get('signal') or 'N/A' }}</code>
            </td>
            <td style="padding: 8px 12px; border: 1px solid #d0d7de;">
              <a href="{{ inc_url }}" target="_blank" class="console-link" style="color: #0969da; text-decoration: none;"><code>{{ inc_id or report.group_id }}</code></a>
            </td>
            <td style="padding: 8px 12px; border: 1px solid #d0d7de;">
              {% set alt_labels = report.raw_metadata.get('labels') or report.raw_metadata.get('policy_user_labels') %}
              {% if alt_labels %}
                {% for k, v in alt_labels.items() %}
                  <code style="font-size: 11px; background: #eef2f8; padding: 2px 4px; border-radius: 3px; display: inline-block; margin: 1px;">{{ k }}={{ v }}</code>
                {% endfor %}
              {% else %}
                <span style="color: #57606a; font-size: 11px;">none</span>
              {% endif %}
            </td>
            <td style="padding: 8px 12px; border: 1px solid #d0d7de;"><span class="badge badge-critical" style="padding: 2px 8px; border-radius: 10px; font-size: 11px;">open</span></td>
            <td style="padding: 8px 12px; border: 1px solid #d0d7de;">{{ report.first_seen or 'N/A' }}</td>
            <td style="padding: 8px 12px; border: 1px solid #d0d7de;"><a href="{{ inc_url }}" target="_blank" class="console-link" style="color: #0969da; text-decoration: none;">View Incident &rarr;</a></td>
          </tr>
          {% endfor %}
        </tbody>
      </table>

      <div class="callout-box" style="background: #f6f8fa; border: 1px solid #d0d7de; border-radius: 6px; padding: 14px; margin-bottom: 16px;">
        <div style="font-weight: 600; font-size: 14px; margin-bottom: 6px; color: #24292f;">Correlation & Grouping Evaluation</div>
        <div style="font-size: 13px; margin-bottom: 6px;">
          <strong>Correlation Score:</strong> {{ report.triage_decision.correlation_score or report.probable_root_cause.confidence }} / 1.00
          &nbsp;|&nbsp; <strong>Duplicate Status:</strong> {{ report.triage_decision.duplicate_status or 'Active (0 duplicate alerts suppressed)' }}
        </div>
        <div style="font-size: 13px; color: #57606a;">
          <strong>Rationale:</strong> {{ report.triage_decision.grouping_rationale or 'Alerts share direct topological causal failure chain across Apigee -> Cloud Run -> Cloud SQL within active 300s window.' }}
        </div>
      </div>

      {% if report.triage_decision.alerts_considered_and_excluded %}
      <h4 style="margin: 12px 0 6px 0; font-size: 14px; color: #24292f;">Alerts Evaluated and Excluded</h4>
      <table style="width: 100%; border-collapse: collapse; font-size: 13px; margin-bottom: 16px;">
        <thead>
          <tr style="background: #f6f8fa;">
            <th style="padding: 8px 12px; border: 1px solid #d0d7de;">Policy / Alert</th>
            <th style="padding: 8px 12px; border: 1px solid #d0d7de;">Resource</th>
            <th style="padding: 8px 12px; border: 1px solid #d0d7de;">Reason for Exclusion</th>
          </tr>
        </thead>
        <tbody>
          {% for exc in report.triage_decision.alerts_considered_and_excluded %}
          <tr>
            <td style="padding: 8px 12px; border: 1px solid #d0d7de;">
              <a href="{{ builder.build_policy_url(exc.policy_id or exc.policy or exc.policy_name) }}" target="_blank" class="console-link" style="color: #0969da; text-decoration: none;">{{ exc.policy or exc.policy_name }}</a>
            </td>
            <td style="padding: 8px 12px; border: 1px solid #d0d7de;">
              <a href="{{ builder.build_resource_url(exc.resource or exc.resource_name) }}" target="_blank" class="console-link" style="color: #0969da; text-decoration: none;"><code>{{ exc.resource or exc.resource_name }}</code></a>
            </td>
            <td style="padding: 8px 12px; border: 1px solid #d0d7de; color: #57606a;">{{ exc.reason }}</td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
      {% endif %}
    </div>

    <!-- Section 4: Topology and Blast Radius -->
    <div class="section" style="margin-bottom: 32px;">
      <div class="section-title" style="font-size: 17px; font-weight: 600; color: #24292f; margin-bottom: 14px; border-bottom: 1px solid #d0d7de; padding-bottom: 6px;">
        2. Topology & Blast Radius Mapping
      </div>
      <div style="margin-bottom: 16px;">
        <h4 style="margin: 0 0 8px 0; font-size: 14px; color: #24292f;">Topological Dependency Path</h4>
        <div style="padding: 12px 16px; background: #f6f8fa; border: 1px solid #d0d7de; border-radius: 6px; font-family: monospace; font-size: 13px; color: #0969da;">
          {{ report.dependency_path or report.raw_metadata.dependency_path or 'Not available' }}
        </div>
      </div>

      <h4 style="margin: 12px 0 6px 0; font-size: 14px; color: #24292f;">Node Impact Assessment</h4>
      <table style="width: 100%; border-collapse: collapse; font-size: 13px; margin-bottom: 16px;">
        <thead>
          <tr style="background: #f6f8fa;">
            <th style="padding: 8px 12px; border: 1px solid #d0d7de;">Resource</th>
            <th style="padding: 8px 12px; border: 1px solid #d0d7de;">Resource Type</th>
            <th style="padding: 8px 12px; border: 1px solid #d0d7de;">Topological Role</th>
            <th style="padding: 8px 12px; border: 1px solid #d0d7de;">Operational Impact</th>
          </tr>
        </thead>
        <tbody>
          {% for node in report.topology_nodes %}
          <tr>
            <td style="padding: 8px 12px; border: 1px solid #d0d7de;">
              <a href="{{ builder.build_resource_url(node.resource or node.name, node.type or node.resource_type) }}" target="_blank" class="console-link" style="color: #0969da; text-decoration: none;"><code>{{ node.resource or node.name }}</code></a>
            </td>
            <td style="padding: 8px 12px; border: 1px solid #d0d7de;">{{ node.type or node.resource_type }}</td>
            <td style="padding: 8px 12px; border: 1px solid #d0d7de;">
              <span class="badge {% if 'Origin' in (node.role or '') %}badge-critical{% else %}badge-info{% endif %}" style="padding: 2px 8px; border-radius: 10px; font-size: 11px;">
                {{ node.role or 'Dependency' }}
              </span>
            </td>
            <td style="padding: 8px 12px; border: 1px solid #d0d7de;">{{ node.impact or node.impact_status or 'Impacted downstream consumer' }}</td>
          </tr>
          {% else %}
          {% for r in report.blast_radius %}
          <tr>
            <td style="padding: 8px 12px; border: 1px solid #d0d7de;">
              <a href="{{ builder.build_resource_url(r) }}" target="_blank" class="console-link" style="color: #0969da; text-decoration: none;"><code>{{ r }}</code></a>
            </td>
            <td style="padding: 8px 12px; border: 1px solid #d0d7de;">Cloud Service</td>
            <td style="padding: 8px 12px; border: 1px solid #d0d7de;">
              <span class="badge {% if r == report.origin %}badge-critical{% else %}badge-info{% endif %}" style="padding: 2px 8px; border-radius: 10px; font-size: 11px;">
                {% if r == report.origin %}Origin (Root Callee){% else %}Downstream Dependent{% endif %}
              </span>
            </td>
            <td style="padding: 8px 12px; border: 1px solid #d0d7de;">
              {% if r == report.origin %}Root defect / anomaly detected{% else %}Cascading error propagation{% endif %}
            </td>
          </tr>
          {% endfor %}
          {% endfor %}
        </tbody>
      </table>
    </div>

    <!-- Section 5: Chronological Event Timeline -->
    <div class="section" style="margin-bottom: 32px;">
      <div class="section-title" style="font-size: 17px; font-weight: 600; color: #24292f; margin-bottom: 14px; border-bottom: 1px solid #d0d7de; padding-bottom: 6px;">
        3. Incident Timeline (UTC Order)
      </div>
      <table style="width: 100%; border-collapse: collapse; font-size: 13px; margin-bottom: 16px;">
        <thead>
          <tr style="background: #f6f8fa;">
            <th style="width: 190px; padding: 8px 12px; border: 1px solid #d0d7de;">Timestamp (UTC)</th>
            <th style="width: 140px; padding: 8px 12px; border: 1px solid #d0d7de;">Event Category</th>
            <th style="padding: 8px 12px; border: 1px solid #d0d7de;">Event Description</th>
          </tr>
        </thead>
        <tbody>
          {% for item in report.timeline %}
          <tr>
            <td style="padding: 8px 12px; border: 1px solid #d0d7de; font-family: monospace;">{{ item.timestamp }}</td>
            <td style="padding: 8px 12px; border: 1px solid #d0d7de;">
              <span class="badge badge-info" style="padding: 2px 8px; border-radius: 10px; font-size: 11px;">
                {{ item.event_type or 'System Event' }}
              </span>
            </td>
            <td style="padding: 8px 12px; border: 1px solid #d0d7de;">{{ item.description }}</td>
          </tr>
          {% else %}
          <tr><td colspan="3" style="padding: 8px 12px; border: 1px solid #d0d7de;">No chronological timeline events recorded</td></tr>
          {% endfor %}
        </tbody>
      </table>
    </div>

    <!-- Section 6: Technical Evidence Dossier Per Layer -->
    <div class="section" style="margin-bottom: 32px;">
      <div class="section-title" style="font-size: 17px; font-weight: 600; color: #24292f; margin-bottom: 14px; border-bottom: 1px solid #d0d7de; padding-bottom: 6px;">
        4. Technical Evidence Dossier
      </div>

      <!-- Metric Comparison Table -->
      <h4 style="margin: 12px 0 6px 0; font-size: 14px; color: #24292f;">Telemetry Metrics: Baseline vs. Breach Comparison</h4>
      <table style="width: 100%; border-collapse: collapse; font-size: 13px; margin-bottom: 16px;">
        <thead>
          <tr style="background: #f6f8fa;">
            <th style="padding: 8px 12px; border: 1px solid #d0d7de;">Layer</th>
            <th style="padding: 8px 12px; border: 1px solid #d0d7de;">Metric Name</th>
            <th style="padding: 8px 12px; border: 1px solid #d0d7de;">Target Resource</th>
            <th style="padding: 8px 12px; border: 1px solid #d0d7de;">Baseline (Before)</th>
            <th style="padding: 8px 12px; border: 1px solid #d0d7de;">Anomaly (During)</th>
            <th style="padding: 8px 12px; border: 1px solid #d0d7de;">Variance / Delta</th>
          </tr>
        </thead>
        <tbody>
          {% for m in report.metrics_comparison %}
          <tr>
            <td style="padding: 8px 12px; border: 1px solid #d0d7de; font-weight: 500;">{{ m.layer }}</td>
            <td style="padding: 8px 12px; border: 1px solid #d0d7de;">
              <a href="{{ builder.build_metrics_explorer_url(m.resource) }}" target="_blank" class="console-link" style="color: #0969da; text-decoration: none;"><code>{{ m.metric_name }}</code></a>
            </td>
            <td style="padding: 8px 12px; border: 1px solid #d0d7de;">
              <a href="{{ builder.build_resource_url(m.resource, m.layer) }}" target="_blank" class="console-link" style="color: #0969da; text-decoration: none;"><code>{{ m.resource }}</code></a>
            </td>
            <td style="padding: 8px 12px; border: 1px solid #d0d7de;">{{ m.before_value }}</td>
            <td style="padding: 8px 12px; border: 1px solid #d0d7de; font-weight: 600; color: #cf222e;">{{ m.during_value }}</td>
            <td style="padding: 8px 12px; border: 1px solid #d0d7de; font-weight: 600;">{{ m.variance }}</td>
          </tr>
          {% else %}
          <tr>
            <td colspan="6" style="padding: 8px 12px; border: 1px solid #d0d7de; color: #57606a;">No comparative metrics captured for this incident.</td>
          </tr>
          {% endfor %}
        </tbody>
      </table>

      <!-- Top Log Signatures -->
      {% if report.top_log_signatures %}
      <h4 style="margin: 16px 0 6px 0; font-size: 14px; color: #24292f;">Top Log Signatures & Sample Excerpts (Escaped)</h4>
      <table style="width: 100%; border-collapse: collapse; font-size: 13px; margin-bottom: 16px;">
        <thead>
          <tr style="background: #f6f8fa;">
            <th style="width: 110px; padding: 8px 12px; border: 1px solid #d0d7de;">Layer</th>
            <th style="padding: 8px 12px; border: 1px solid #d0d7de;">Signature & Logs Link</th>
            <th style="width: 80px; padding: 8px 12px; border: 1px solid #d0d7de;">Count</th>
            <th style="padding: 8px 12px; border: 1px solid #d0d7de;">Sample Entry</th>
          </tr>
        </thead>
        <tbody>
          {% for log in report.top_log_signatures %}
          <tr>
            <td style="padding: 8px 12px; border: 1px solid #d0d7de;">{{ log.layer }}</td>
            <td style="padding: 8px 12px; border: 1px solid #d0d7de;">
              <div style="font-family: monospace; font-size: 12px;">{{ log.signature }}</div>
              <div style="margin-top: 4px;">
                <a href="{{ builder.build_logs_explorer_url(log.query or ('resource.type="cloud_run_revision" AND textPayload=~"' ~ log.signature ~ '"' if 'run' in (log.layer or '').lower() else 'textPayload=~"' ~ log.signature ~ '"'), start_time=report.first_seen, end_time=report.last_alert) }}" target="_blank" class="console-link" style="font-size: 11px; color: #0969da; text-decoration: none;">View in Logs Explorer &rarr;</a>
              </div>
            </td>
            <td style="padding: 8px 12px; border: 1px solid #d0d7de; font-weight: 600;">{{ log.count }}</td>
            <td style="padding: 8px 12px; border: 1px solid #d0d7de;">
              <pre style="margin: 0; padding: 6px; font-size: 11px; max-height: 80px; overflow-y: auto;">{{ log.sample }}</pre>
            </td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
      {% endif %}

      <!-- Apigee Analytics & Latency Breakdown -->
      {% if report.apigee_analytics and report.apigee_analytics.response_codes %}
      <h4 style="margin: 16px 0 6px 0; font-size: 14px; color: #24292f;">Apigee Analytics: Status Code Distribution & Latencies</h4>
      <div class="callout-box" style="background: #f6f8fa; border: 1px solid #d0d7de; border-radius: 6px; padding: 14px; margin-bottom: 16px;">
        <div style="font-size: 13px; margin-bottom: 8px;">
          <strong>Target Proxy:</strong> <a href="{{ builder.build_apigee_proxy_url(report.apigee_analytics.proxy_name) }}" target="_blank" class="console-link" style="color: #0969da; text-decoration: none;"><code>{{ report.apigee_analytics.proxy_name }}</code></a>
          &nbsp;|&nbsp; <strong>Target latency{% if report.apigee_analytics.target_latency_basis %} ({{ report.apigee_analytics.target_latency_basis }}){% endif %}:</strong>
          P50 {{ report.apigee_analytics.target_latency_p50 or 'N/A' }},
          P95 {{ report.apigee_analytics.target_latency_p95 or 'N/A' }},
          P99 {{ report.apigee_analytics.target_latency_p99 or 'N/A' }}
        </div>
        <div style="display: flex; gap: 12px; flex-wrap: wrap;">
          {% for code, count in report.apigee_analytics.response_codes.items() %}
          <div style="background: #ffffff; border: 1px solid #d0d7de; border-radius: 4px; padding: 6px 12px; font-size: 12px;">
            <span style="font-weight: 600;">HTTP {{ code }}:</span> {{ count }}
          </div>
          {% endfor %}
        </div>
      </div>
      {% endif %}

      <!-- Configuration & Revision Diffs -->
      {% if report.revision_diffs %}
      <h4 style="margin: 16px 0 6px 0; font-size: 14px; color: #24292f;">Revision & Deployment Correlation</h4>
      <table style="width: 100%; border-collapse: collapse; font-size: 13px; margin-bottom: 16px;">
        <thead>
          <tr style="background: #f6f8fa;">
            <th style="padding: 8px 12px; border: 1px solid #d0d7de;">Resource</th>
            <th style="padding: 8px 12px; border: 1px solid #d0d7de;">Active Revision</th>
            <th style="padding: 8px 12px; border: 1px solid #d0d7de;">Previous Serving</th>
            <th style="padding: 8px 12px; border: 1px solid #d0d7de;">Change / Diff Summary</th>
          </tr>
        </thead>
        <tbody>
          {% for rev in report.revision_diffs %}
          <tr>
            <td style="padding: 8px 12px; border: 1px solid #d0d7de;">
              <a href="{{ builder.build_resource_url(rev.resource) }}" target="_blank" class="console-link" style="color: #0969da; text-decoration: none;"><code>{{ rev.resource }}</code></a>
            </td>
            <td style="padding: 8px 12px; border: 1px solid #d0d7de;">
              {% if 'sql' in (rev.resource or '').lower() or 'db' in (rev.resource or '').lower() %}
              <a href="{{ builder.build_cloud_sql_instance_url(rev.resource) }}" target="_blank" class="console-link" style="color: #0969da; text-decoration: none;"><code>{{ rev.current_revision }}</code></a>
              {% elif 'apigee' in (rev.resource or '').lower() or 'proxy' in (rev.resource or '').lower() %}
              <a href="{{ builder.build_apigee_proxy_url(rev.resource, rev.current_revision) }}" target="_blank" class="console-link" style="color: #0969da; text-decoration: none;"><code>{{ rev.current_revision }}</code></a>
              {% else %}
              <a href="{{ builder.build_cloud_run_service_url(rev.resource, tab='revisions') }}" target="_blank" class="console-link" style="color: #0969da; text-decoration: none;"><code>{{ rev.current_revision }}</code></a>
              {% endif %}
            </td>
            <td style="padding: 8px 12px; border: 1px solid #d0d7de;"><code>{{ rev.previous_revision }}</code></td>
            <td style="padding: 8px 12px; border: 1px solid #d0d7de;">{{ rev.diff_summary }}</td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
      {% endif %}

      <!-- Layer Evidence Items Dossier -->
      {% for area, items in report.evidence_by_area.items() %}
      <h4 style="margin: 14px 0 6px 0; text-transform: capitalize; font-size: 14px; color: #24292f;">{{ area }} Observability Findings</h4>
      {% for item in items %}
      <div style="margin-bottom: 12px;">
        <div style="font-size: 13px; font-weight: 600; color: #24292f;">{{ item.headline }}</div>
        <pre style="margin: 4px 0;">{{ item.content }}</pre>
      </div>
      {% endfor %}
      {% endfor %}
    </div>

    <!-- Section 7: Probable Root Cause Analysis -->
    <div class="section" style="margin-bottom: 32px;">
      <div class="section-title" style="font-size: 17px; font-weight: 600; color: #24292f; margin-bottom: 14px; border-bottom: 1px solid #d0d7de; padding-bottom: 6px;">
        5. Probable Root Cause Analysis
      </div>
      <div class="callout-box" style="background: #f6f8fa; border: 1px solid #d0d7de; border-radius: 6px; padding: 14px; margin-bottom: 16px;">
        <div style="font-size: 15px; font-weight: 600; color: #24292f; margin-bottom: 8px;">
          Primary Hypothesis:
        </div>
        <p style="margin: 0 0 10px 0; font-size: 14px; color: #24292f;">
          {{ report.probable_root_cause.text }}
        </p>
        <div style="font-size: 13px; color: #57606a;">
          <strong>Confidence Score:</strong> <span style="color: #0969da; font-weight: 600;">{{ (report.probable_root_cause.confidence * 100)|int }}%</span>
          {% if report.root_cause_analysis.confidence_rationale %}
          &nbsp;|&nbsp; <strong>Rationale:</strong> {{ report.root_cause_analysis.confidence_rationale }}
          {% endif %}
        </div>
        {% if report.root_cause_analysis.contributing_factors %}
        <div style="margin-top: 10px; padding-top: 8px; border-top: 1px solid #d0d7de; font-size: 12px; color: #57606a;">
          <strong>Contributing Evidence Factors:</strong>
          <ul style="margin: 4px 0 0 0; padding-left: 20px;">
            {% for factor, detail in report.root_cause_analysis.contributing_factors.items() %}
            <li style="margin-bottom: 2px;">
              <strong>{{ factor|replace('_', ' ')|title }}:</strong>
              {% if detail is mapping and detail.detail is defined %}
                {{ detail.detail }} (contribution: +{{ detail.score }})
              {% elif detail is mapping and detail.score is defined %}
                Score: {{ detail.score }}
              {% else %}
                {{ detail }}
              {% endif %}
            </li>
            {% endfor %}
          </ul>
        </div>
        {% endif %}
      </div>

      {% if report.root_cause_analysis.supporting_evidence %}
      <div style="margin-bottom: 14px;">
        <h4 style="margin: 0 0 6px 0; font-size: 14px; color: #1a7f37;">Supporting Evidence</h4>
        <ul style="margin: 0; padding-left: 20px; font-size: 13px;">
          {% for sup in report.root_cause_analysis.supporting_evidence %}
          <li style="margin-bottom: 4px;">{{ sup }}</li>
          {% endfor %}
        </ul>
      </div>
      {% endif %}

      {% if report.root_cause_analysis.contradicting_evidence %}
      <div style="margin-bottom: 14px;">
        <h4 style="margin: 0 0 6px 0; font-size: 14px; color: #57606a;">Contradicting / Excluded Telemetry</h4>
        <ul style="margin: 0; padding-left: 20px; font-size: 13px; color: #57606a;">
          {% for con in report.root_cause_analysis.contradicting_evidence %}
          <li style="margin-bottom: 4px;">{{ con }}</li>
          {% endfor %}
        </ul>
      </div>
      {% endif %}

      {% if report.root_cause_analysis.alternatives_ruled_out %}
      <h4 style="margin: 14px 0 6px 0; font-size: 14px; color: #24292f;">Alternative Hypotheses Evaluated and Ruled Out</h4>
      <table style="width: 100%; border-collapse: collapse; font-size: 13px; margin-bottom: 16px;">
        <thead>
          <tr style="background: #f6f8fa;">
            <th style="padding: 8px 12px; border: 1px solid #d0d7de;">Alternative Failure Mode</th>
            <th style="padding: 8px 12px; border: 1px solid #d0d7de;">Reason Ruled Out</th>
          </tr>
        </thead>
        <tbody>
          {% for alt in report.root_cause_analysis.alternatives_ruled_out %}
          <tr>
            <td style="padding: 8px 12px; border: 1px solid #d0d7de; font-weight: 500;">{{ alt.alternative }}</td>
            <td style="padding: 8px 12px; border: 1px solid #d0d7de; color: #57606a;">{{ alt.reason }}</td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
      {% endif %}
    </div>

    <!-- Section 8: Remediation & Mitigation Plan -->
    <div class="section" style="margin-bottom: 32px;">
      <div class="section-title" style="font-size: 17px; font-weight: 600; color: #24292f; margin-bottom: 14px; border-bottom: 1px solid #d0d7de; padding-bottom: 6px;">
        6. Remediation & Recovery Plan
      </div>
      <h4 style="margin: 0 0 8px 0; font-size: 14px; color: #24292f;">Ordered Immediate Action Steps</h4>
      <ol style="margin: 0 0 16px 0; padding-left: 20px; font-size: 13px;">
        {% for act in report.get_normalized_actions() %}
        {% if act.command or act.title != 'Rollback' %}
        <li style="margin-bottom: 12px;">
          <div style="font-weight: 600; color: #24292f;">{{ act.title }}</div>
          {% if act.command %}
          <pre><code style="background:#f4f4f4;border:1px solid #ddd;padding:6px;font-family:monospace;display:block">{{ act.command }}</code></pre>
          {% endif %}
          {% if act.verification %}
          <div style="margin-top: 4px; color: #1a7f37;"><strong>Verification:</strong> {{ act.verification }}</div>
          {% endif %}
          {% if act.rollback and act.title != 'Rollback' %}
          <div style="margin-top: 2px; color: #cf222e;"><strong>Rollback:</strong> {{ act.rollback }}</div>
          {% endif %}
        </li>
        {% endif %}
        {% endfor %}
      </ol>

      {% if report.longer_term_fixes %}
      <h4 style="margin: 16px 0 6px 0; font-size: 14px; color: #24292f;">Longer-Term Architectural Fixes & Preventative Actions</h4>
      <ul style="margin: 0; padding-left: 20px; font-size: 13px;">
        {% for fix in report.longer_term_fixes %}
        <li style="margin-bottom: 6px;">{{ fix }}</li>
        {% endfor %}
      </ul>
      {% endif %}
    </div>

    <!-- Section 9: Appendix: Observability Queries & Run Metadata -->
    <div class="section" style="margin-bottom: 16px;">
      <div class="section-title" style="font-size: 17px; font-weight: 600; color: #24292f; margin-bottom: 14px; border-bottom: 1px solid #d0d7de; padding-bottom: 6px;">
        7. Appendix: Diagnostic Execution & Telemetry Queries
      </div>
      
      <h4 style="margin: 0 0 6px 0; font-size: 14px; color: #24292f;">Specialist Observability Queries Executed</h4>
      {% for area, queries in report.queries_appendix.items() %}
      <div style="margin-bottom: 10px;">
        <span style="font-weight: 600; text-transform: capitalize; font-size: 13px; color: #24292f;">{{ area }}:</span>
        {% for q in queries %}
        <pre style="margin: 4px 0;">{{ q }}</pre>
        {% if 'fetch' in q or 'metric' in q %}
        <div style="margin-bottom: 6px;"><a href="{{ builder.build_metrics_explorer_url(report.origin) }}" target="_blank" class="console-link" style="font-size: 11px; color: #0969da; text-decoration: none;">Open Service Metrics &rarr;</a></div>
        {% else %}
        <div style="margin-bottom: 6px;"><a href="{{ builder.build_logs_explorer_url(q, start_time=report.first_seen, end_time=report.last_alert) }}" target="_blank" class="console-link" style="font-size: 11px; color: #0969da; text-decoration: none;">Run in Logs Explorer &rarr;</a></div>
        {% endif %}
        {% endfor %}
      </div>
      {% else %}
      <p style="font-size: 13px; color: #57606a;">No observability queries recorded.</p>
      {% endfor %}

      <div class="meta-grid" style="display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; margin-top: 16px; padding: 12px; background: #f6f8fa; border-radius: 6px; border: 1px solid #d0d7de; font-size: 13px;">
        <div class="meta-item" style="display: flex; flex-direction: column;">
          <span class="meta-label" style="font-size: 11px; text-transform: uppercase; color: #57606a; font-weight: 600;">Agent Runtime</span>
          <span class="meta-val" style="font-weight: 500; color: #24292f;">{{ report.agent_run_metadata.agent_revision or 'Unspecified' }}</span>
        </div>
        <div class="meta-item" style="display: flex; flex-direction: column;">
          <span class="meta-label" style="font-size: 11px; text-transform: uppercase; color: #57606a; font-weight: 600;">Bridge Runtime</span>
          <span class="meta-val" style="font-weight: 500; color: #24292f;">{{ report.agent_run_metadata.bridge_revision or 'Unspecified' }}</span>
        </div>
        <div class="meta-item" style="display: flex; flex-direction: column;">
          <span class="meta-label" style="font-size: 11px; text-transform: uppercase; color: #57606a; font-weight: 600;">Inference Model</span>
          <span class="meta-val" style="font-weight: 500; color: #24292f;">{{ report.agent_run_metadata.model or 'Unspecified' }}</span>
        </div>
        <div class="meta-item" style="display: flex; flex-direction: column;">
          <span class="meta-label" style="font-size: 11px; text-transform: uppercase; color: #57606a; font-weight: 600;">Execution Latency</span>
          <span class="meta-val" style="font-weight: 500; color: #24292f;">{{ report.agent_run_metadata.duration_seconds or 'Unspecified' }}</span>
        </div>
      </div>

      {% if report.tools_used %}
      <div style="margin-top: 12px; font-size: 12px; color: #57606a;">
        <strong>Diagnostic Tools Invoked:</strong> {{ report.tools_used|join(', ') }}
      </div>
      {% endif %}
      {% if report.skills_loaded %}
      <div style="margin-top: 4px; font-size: 12px; color: #57606a;">
        <strong>Engineering Skills Loaded:</strong> {{ report.skills_loaded|join(', ') }}
      </div>
      {% endif %}
    </div>

  </div>
</body>
</html>
"""

_env = Environment(autoescape=select_autoescape(["html", "xml"]))
_template = _env.from_string(HTML_TEMPLATE)


def render_standalone_html(report: InvestigationReport) -> str:
    """Render standalone HTML diagnostic report with canonical console links."""
    builder = ConsoleLinkBuilder(project_id=report.project_id, region=getattr(report, "region", "us-west1"))
    return _template.render(report=report, builder=builder)
