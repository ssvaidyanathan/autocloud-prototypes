---
name: test-failure-triage
description: Triage and diagnosis of automated unit, integration, and end-to-end test failures
---

# Test Failure Triage Runbook

## Diagnostic Workflow
1. **Identify Test Framework**: Detect `pytest`, `unittest`, `jest`, `mocha`, `go test`.
2. **Extract Failure Signatures**:
   - Failing test case node id (e.g., `tests/test_api.py::test_create_endpoint`)
   - Assertion failure error message (e.g., `assert 400 == 200`)
   - Bottom frame of Python / JavaScript traceback.
3. **Inspect Code Context**:
   - Fetch the file and test definition using `get_file_content`.
   - Inspect commit diff (`get_commit_diff`) to see what logic changed in the pull request.
4. **Formulate Recommended Remediation**:
   - Provide exact local command to isolate the test: `pytest -k <test_name> -vv`.
   - Distinguish regression vs broken test mock vs intended API signature change.
