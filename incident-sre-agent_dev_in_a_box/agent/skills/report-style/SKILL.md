---
name: report-style
description: SRE report writing guidelines, tone, confidence definitions, and action item conventions.
---

# SRE Incident Report Style Guide

## Tone & Philosophy
- **Blameless & Objective**: Focus on system dynamics, code paths, telemetry evidence, and configurations, never human error.
- **Strictly Evidence-Backed**: Every statement regarding causality, error status, or remediation must reference concrete telemetry evidence (exact HTTP status codes, log excerpts, metric shifts, revision SHAs, author emails). Never assert ungrounded facts missing from the evidence.
- **Mitigation Grounding**: Mitigation must NEVER be asserted as a proven fact unless evidence within the investigation window proves that errors ceased after the change. If post-deployment telemetry is absent or outside the window, mitigation must be stated as provisional/unverified, and recorded in `caveats`.
- **Concise & Actionable**: Avoid boilerplate fluff; prioritize executive clarity and precise engineering next steps.
- **Conflict & Uncertainty Transparency**: When specialist telemetry exhibits discrepancies (e.g. error logs observed while metric error rate is 0.0, or all revisions show 0% traffic), explicitly state the conflict and acknowledge uncertainty.

## Confidence & Probability Calibration
- **High Confidence (0.80 - 1.00)**: Direct causal link verified with correlated telemetry across domains (e.g. revision deploy by identified author matched with immediate non-zero HTTP 5xx log excerpts and metric rate elevation).
- **Medium Confidence (0.50 - 0.79)**: Strong temporal correlation with plausible mechanism, but missing direct confirmation (e.g. error logs present but metrics unaligned, or deploy occurred but stack trace lacks revision tag, or mitigation is provisional).
- **Low Confidence (< 0.50)**: Speculative hypothesis based on peripheral symptoms, conflicting indicators, zero error samples, or incomplete telemetry. Confidence MUST be calibrated below 0.50 whenever key evidence areas are empty or uncorroborated.

## Report Structure & Required Fields
All reports must strictly populate:
1. **Executive Summary**: 2-3 sentences stating what happened, user impact, and resolution status, citing confirmed symptoms (stating provisional mitigation if unverified).
2. **Timeline of Events**: Chronological breakdown from change onset to symptom detection, directly citing evidence timestamps.
3. **Probable Root Cause**: Grounded, descriptive prose explaining the causal mechanism, triggering change (with revision, author, timestamp), and blast radius. Pure LLM prose without fallback templates, truncated fields, or concatenated calibration notes (text and confidence MUST always be populated).
4. **Evidence & Telemetry**: Direct excerpts, HTTP status codes, and metric charts with Cloud Console links.
5. **Recommended Action Items**: Evidence-specific remediation steps phrased with active verbs (MUST cite exact service names, faulty vs baseline revision comparison, specific endpoints from error logs, and post-mitigation telemetry verification on active revision; NEVER use generic boilerplate).
6. **Specialists Status**: Status entries for all specialists (logs, metrics, changes, resources).
7. **Skills Used**: Record all skills actually loaded and consulted during the investigation.
8. **Caveats**: Explicit list of caveats, unverified claims (e.g. provisional post-deployment mitigation), or telemetry gaps.
