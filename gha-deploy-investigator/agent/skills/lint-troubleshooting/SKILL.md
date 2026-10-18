---
name: lint-troubleshooting
description: Troubleshooting static analysis, style enforcement, and linter violations in CI pipelines
---

# Linter Troubleshooting Runbook

## Diagnostic Workflow
1. **Identify the Tool**: Detect which linter failed (Ruff, Flake8, Mypy, ESLint, Prettier).
2. **Parse Diagnostic Coordinates**:
   - File path
   - Line number and column
   - Rule ID (e.g., `F841`, `E501`, `no-unused-vars`)
   - Violation description
3. **Correlate with Commit Changes**:
   - Check if the violation occurs on a line modified in the commit diff (`get_commit_diff`).
   - If not, check if a global rule was modified (e.g. `pyproject.toml`, `.eslintrc`).
4. **Formulate Recommended Remediation**:
   - Suggest exact command for automatic formatting / fixing (e.g., `ruff check --fix`, `eslint --fix`).
   - If manual fix is needed, generate minimal syntax fix.
