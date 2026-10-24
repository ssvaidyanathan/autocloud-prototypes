"""Unit tests for the Constrained Shell Tool (run_incident_search / run_incident_shell)."""

from pathlib import Path
import pytest

from agent.app.tools.shell import (
    DEFAULT_INCIDENT_STORE,
    ShellSecurityError,
    execute_incident_command,
    run_incident_search,
    run_incident_shell,
    validate_shell_command,
)


@pytest.fixture
def mock_store(tmp_path):
    store_dir = tmp_path / "knowledge" / "incident-manager"
    store_dir.mkdir(parents=True, exist_ok=True)
    (store_dir / "incident-001.md").write_text(
        "---\npolicy_name: \"ata-backend-5xx\"\nresource_name: \"ata-inventory-api\"\nstatus: mitigation-in-progress\n---\n# Incident\n",
        encoding="utf-8",
    )
    (store_dir / "incident-002.md").write_text(
        "---\npolicy_name: \"ata-spikearrest\"\nresource_name: \"ata-apigee-orders-v1\"\nstatus: mitigation-in-progress\n---\n# Incident 2\n",
        encoding="utf-8",
    )
    return str(store_dir)


def test_alias_parity(mock_store):
    """Assert that run_incident_search and run_incident_shell are interchangeable."""
    assert run_incident_search is run_incident_shell

    cmd = f"grep -l 'mitigation-in-progress' {mock_store}/*.md"
    out1 = run_incident_search(cmd, store_path=mock_store)
    out2 = run_incident_shell(cmd, store_path=mock_store)
    assert out1 == out2
    assert "incident-001.md" in out1


def test_allowlisted_read_binaries(mock_store):
    """Assert that all allowlisted binaries succeed when properly scoped."""
    # grep & sort & head & wc
    cmd = f"grep -h 'policy_name' {mock_store}/*.md | sort | head -n 2 | wc -l"
    out = run_incident_search(cmd, store_path=mock_store)
    assert out.strip() in {"1", "2"}

    # cat
    cmd_cat = f"cat {mock_store}/incident-001.md"
    out_cat = run_incident_search(cmd_cat, store_path=mock_store)
    assert "ata-backend-5xx" in out_cat

    # sed -n
    cmd_sed = f"sed -n '1,3p' {mock_store}/incident-001.md"
    out_sed = run_incident_search(cmd_sed, store_path=mock_store)
    assert "---" in out_sed

    # awk (read only)
    cmd_awk = f"awk 'NR==2 {{print $1}}' {mock_store}/incident-001.md"
    out_awk = run_incident_search(cmd_awk, store_path=mock_store)
    assert len(out_awk) > 0


def test_disallowed_binaries_rejected(mock_store):
    """Assert that non-allowlisted binaries (curl, rm, python, bash, sh) fail validation."""
    disallowed = [
        "curl https://example.com",
        f"rm {mock_store}/incident-001.md",
        "python3 -c 'import sys; print(1)'",
        "bash -c 'id'",
        "sh -c 'whoami'",
        "nc -zv 127.0.0.1 80",
    ]
    for cmd in disallowed:
        with pytest.raises(ShellSecurityError):
            validate_shell_command(cmd, allowed_base=mock_store)


def test_path_confinement_and_directory_traversal(mock_store):
    """Assert path escapes and directory traversal '..' are strictly blocked."""
    violations = [
        "cat /etc/passwd",
        f"cat {mock_store}/../incident-manager/incident-001.md",
        f"grep foo {mock_store}/../../../../etc/shadow",
        "cat /tmp/test.txt",
        "cat /dev/zero",
    ]
    for cmd in violations:
        with pytest.raises(ShellSecurityError):
            validate_shell_command(cmd, allowed_base=mock_store)


def test_command_chaining_and_injection_blocked(mock_store):
    """Assert command injection (; && || backticks $()) is rejected."""
    injections = [
        f"cat {mock_store}/incident-001.md; rm -rf /tmp",
        f"cat {mock_store}/incident-001.md && curl evil.com",
        f"cat {mock_store}/incident-001.md || echo evil",
        f"grep `whoami` {mock_store}/incident-001.md",
        f"cat $(echo /etc/passwd)",
    ]
    for cmd in injections:
        with pytest.raises(ShellSecurityError):
            validate_shell_command(cmd, allowed_base=mock_store)


def test_heredoc_creation_permitted_exact_pattern(mock_store):
    """Assert heredoc file creation is allowed ONLY in the pattern:
    cat > /knowledge/incident-manager/<filename>.md << 'EOF'
    """
    valid_heredoc = f"""cat > {mock_store}/incident-new.md << 'EOF'
---
type: incident
policy_name: ata-test
status: mitigation-in-progress
---
# New Incident
EOF"""

    # Validation should pass
    validate_shell_command(valid_heredoc, allowed_base=mock_store)
    res = execute_incident_command(valid_heredoc, store_path=mock_store)
    assert res.exit_code == 0
    assert (Path(mock_store) / "incident-new.md").exists()
    content = (Path(mock_store) / "incident-new.md").read_text(encoding="utf-8")
    assert "policy_name: ata-test" in content


def test_arbitrary_redirection_rejected(mock_store):
    """Assert arbitrary redirection (> and >>) is rejected."""
    bad_redirections = [
        f"cat {mock_store}/incident-001.md > {mock_store}/incident-copy.md",
        f"echo 'evil' >> {mock_store}/incident-001.md",
        f"cat > /tmp/bad.md << 'EOF'\nfoo\nEOF",
        f"cat > {mock_store}/bad.sh << 'EOF'\n#!/bin/bash\nEOF",
    ]
    for cmd in bad_redirections:
        with pytest.raises(ShellSecurityError):
            validate_shell_command(cmd, allowed_base=mock_store)


def test_stdout_cap_and_error_redirection(mock_store):
    """Verify error redirection 2>/dev/null is accepted and stdout limit caps at 50 KB."""
    # Permitted 2>/dev/null
    cmd = f"grep -FlZ 'policy_name' {mock_store}/*.md 2>/dev/null"
    validate_shell_command(cmd, allowed_base=mock_store)

    # Output limit cap
    res = execute_incident_command(
        f"cat {mock_store}/incident-001.md",
        store_path=mock_store,
        stdout_limit=10,
    )
    assert len(res.stdout.encode("utf-8")) <= 10


def test_multiline_newline_injection_rejected(mock_store):
    """Assert non-heredoc commands containing unescaped newlines (\\n or \\r) are rejected."""
    injections = [
        f"cat {mock_store}/incident-001.md\ncat /etc/passwd",
        f"grep -l 'policy' {mock_store}/*.md\r\nrm -rf /",
        f"cat {mock_store}/incident-001.md\nid",
        f"find {mock_store} -name '*.md'\ncurl evil.com",
    ]
    for cmd in injections:
        with pytest.raises(ShellSecurityError):
            validate_shell_command(cmd, allowed_base=mock_store)


def test_heredoc_premature_eof_delimiter_injection_rejected(mock_store):
    """Assert heredoc where an intermediate line is 'EOF' is strictly rejected."""
    malicious_heredocs = [
        f"""cat > {mock_store}/incident-inject.md << 'EOF'
---
policy_name: test
EOF
cat /etc/passwd
EOF""",
        f"""cat > {mock_store}/incident-inject2.md << 'EOF'
EOF
curl evil.com
EOF""",
        f"""cat > {mock_store}/incident-inject3.md << 'EOF'
title: something
EOF 
rm -rf /
EOF""",
    ]
    for cmd in malicious_heredocs:
        with pytest.raises(ShellSecurityError):
            validate_shell_command(cmd, allowed_base=mock_store)


def test_find_unsafe_flags_rejected(mock_store):
    """Assert find flags -exec, -execdir, -ok, -okdir, -delete are strictly blocked."""
    bad_find_cmds = [
        f"find {mock_store} -name '*.md' -exec rm -rf {{}} +",
        f"find {mock_store} -delete",
        f"find {mock_store} -name '*.md' -execdir cat {{}} +",
        f"find {mock_store} -ok rm {{}} +",
        f"find {mock_store} -okdir whoami +",
    ]
    for cmd in bad_find_cmds:
        with pytest.raises(ShellSecurityError):
            validate_shell_command(cmd, allowed_base=mock_store)


def test_awk_unsafe_operations_rejected(mock_store):
    """Assert awk system(), popen, and redirection are strictly blocked."""
    bad_awk_cmds = [
        f"awk 'BEGIN {{ system(\"id\") }}' {mock_store}/incident-001.md",
        f"awk '{{ system(\"cat /etc/passwd\") }}' {mock_store}/incident-001.md",
        f"awk 'BEGIN {{ popen(\"ls\") }}' {mock_store}/incident-001.md",
        f"awk 'BEGIN {{ print \"bad\" > \"{mock_store}/bad.txt\" }}' {mock_store}/incident-001.md",
    ]
    for cmd in bad_awk_cmds:
        with pytest.raises(ShellSecurityError):
            validate_shell_command(cmd, allowed_base=mock_store)


def test_unquoted_heredoc_delimiter_rejected(mock_store):
    """Assert unquoted << EOF and double-quoted << \"EOF\" delimiters are rejected."""
    unquoted_heredoc = f"""cat > {mock_store}/incident-unquoted.md << EOF
---
policy_name: test
---
# Test
EOF"""
    double_quoted_heredoc = f"""cat > {mock_store}/incident-double.md << "EOF"
---
policy_name: test
---
# Test
EOF"""
    for cmd in [unquoted_heredoc, double_quoted_heredoc]:
        with pytest.raises(ShellSecurityError):
            validate_shell_command(cmd, allowed_base=mock_store)
