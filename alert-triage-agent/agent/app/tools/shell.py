"""Constrained Shell Tool for Alert Triage Agent.

Provides run_incident_search and registered alias run_incident_shell.
Enforces strict binary allowlist, path confinement to /knowledge/incident-manager,
error redirection 2>/dev/null, permitted heredoc creation, execution timeout,
and stdout limit.
"""

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shlex
import subprocess
from typing import Optional

# Binaries permitted in read pipelines
READ_BINARY_ALLOWLIST = {
    "grep",
    "find",
    "xargs",
    "head",
    "tail",
    "cat",
    "sort",
    "wc",
    "date",
    "sed",
    "awk",
}

DEFAULT_INCIDENT_STORE = "/knowledge/incident-manager"
EXECUTION_TIMEOUT_SECONDS = 10
STDOUT_LIMIT_BYTES = 50 * 1024  # 50 KB


class ShellSecurityError(ValueError):
    """Raised when a command violates the constrained shell security rules."""
    pass


@dataclass
class ShellExecutionResult:
    stdout: str
    stderr: str
    exit_code: int


def _validate_path_confinement(token: str, allowed_base: str = DEFAULT_INCIDENT_STORE) -> None:
    """Validate that any path-like argument is strictly confined to allowed_base."""
    # Check directory traversal
    if ".." in token:
        raise ShellSecurityError(f"Directory traversal '..' is strictly forbidden: {token}")

    # Strip surrounding quotes if any
    cleaned = token.strip("\"'")

    # Ignore flags
    if cleaned.startswith("-"):
        return

    # Allow /dev/null
    if cleaned == "/dev/null":
        return

    norm_allowed = os.path.normpath(allowed_base)
    # Handle wildcard patterns like /path/*.md
    check_target = re.sub(r"[\*\?].*$", "", cleaned).rstrip(os.sep)
    norm_path = os.path.normpath(check_target) if check_target else norm_allowed

    # If the path is strictly within allowed_base, accept it
    if norm_path == norm_allowed or norm_path.startswith(norm_allowed + os.sep):
        return

    # Check for forbidden system directories
    forbidden_prefixes = (
        "/etc", "/tmp", "/var", "/usr", "/bin", "/sbin", "/proc", "/sys",
        "/home", "/root", "/boot", "/opt", "/lib", "/dev",
    )
    for pfx in forbidden_prefixes:
        if cleaned == pfx or cleaned.startswith(pfx + "/"):
            raise ShellSecurityError(f"Path outside allowed directory is forbidden: {token}")

    if cleaned.startswith("/"):
        raise ShellSecurityError(f"Path is not confined to {allowed_base}: {token}")


def _validate_heredoc(command: str, allowed_base: str = DEFAULT_INCIDENT_STORE) -> None:
    """Validate heredoc file creation syntax.
    
    Permitted format:
    cat > /knowledge/incident-manager/<filename>.md << 'EOF'
    ...
    EOF
    """
    lines = command.strip().splitlines()
    if not lines or len(lines) < 2:
        raise ShellSecurityError("Incomplete heredoc command")

    first_line = lines[0].strip()
    # Match: cat > <allowed_base>/<name>.md << 'EOF' (strict single-quoted delimiter required)
    pattern = rf"^cat\s+>\s+({re.escape(allowed_base)}/[a-zA-Z0-9_\-.]+\.md)\s+<<\s*'EOF'$"
    match = re.match(pattern, first_line)
    if not match:
        raise ShellSecurityError(
            f"Invalid heredoc header: must strictly match 'cat > {allowed_base}/<filename>.md << \\'EOF\\''"
        )

    target_path = match.group(1)
    if ".." in target_path or not target_path.endswith(".md"):
        raise ShellSecurityError(f"Invalid target file in heredoc: {target_path}")

    # Reject any heredoc where an intermediate line (between line 1 and the last line) is "EOF"
    for intermediate in lines[1:-1]:
        if intermediate.strip() == "EOF":
            raise ShellSecurityError("Heredoc contains premature EOF marker on intermediate line")

    # Verify terminating EOF
    if lines[-1].strip() != "EOF":
        raise ShellSecurityError("Heredoc missing terminating EOF marker")


def _split_pipeline(command_str: str) -> list[str]:
    """Split a command into pipeline stages, preserving pipes inside quotes."""
    stages: list[str] = []
    current: list[str] = []
    in_single = False
    in_double = False
    escaped = False

    for ch in command_str:
        if escaped:
            current.append(ch)
            escaped = False
            continue
        if ch == "\\":
            current.append(ch)
            escaped = True
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            current.append(ch)
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            current.append(ch)
            continue
        if ch == "|" and not in_single and not in_double:
            stages.append("".join(current))
            current = []
            continue
        current.append(ch)

    stages.append("".join(current))
    return stages


def validate_shell_command(command: str, allowed_base: str = DEFAULT_INCIDENT_STORE) -> None:
    """Validate a shell command against the security policy.
    
    Raises ShellSecurityError if any policy violation is detected.
    """
    if not command or not command.strip():
        raise ShellSecurityError("Empty command string")

    # Check if this is a permitted heredoc before stripping line breaks
    if "<<" in command:
        if command.strip().startswith("cat >"):
            _validate_heredoc(command, allowed_base)
            return
        raise ShellSecurityError("Heredoc '<<' only permitted with 'cat > /knowledge/incident-manager/<filename>.md'")

    # Reject non-heredoc commands containing unescaped newlines (\n or \r)
    i = 0
    n = len(command)
    while i < n:
        if command[i] in ("\n", "\r"):
            bs_count = 0
            j = i - 1
            while j >= 0 and command[j] == "\\":
                bs_count += 1
                j -= 1
            if bs_count % 2 == 0:
                raise ShellSecurityError("Unescaped newline characters ('\\n' or '\\r') are forbidden in non-heredoc commands")
            if command[i] == "\r" and i + 1 < n and command[i + 1] == "\n":
                i += 1
        i += 1

    # Normalize line continuations for pipeline validation
    command_normalized = re.sub(r"\\\r?\n", " ", command)
    stripped = command_normalized.strip()

    # Check for command chaining and command substitution
    forbidden_meta = [";", "&&", "||", "`", "$("]
    for meta in forbidden_meta:
        if meta in stripped:
            raise ShellSecurityError(f"Command separator or substitution '{meta}' is forbidden")

    # Reject background operator (& not part of 2>&1)
    if re.search(r"(?<!2>)&(?!1)", stripped):
        raise ShellSecurityError("Background execution '&' is forbidden")

    # Check for arbitrary output redirection (> or >>)
    if ">" in stripped:
        clean_without_err = re.sub(r"2>\s*/dev/null", "", stripped)
        if ">" in clean_without_err:
            raise ShellSecurityError("Output redirection ('>', '>>') is forbidden outside permitted heredoc")

    # Check for input redirection (<)
    if "<" in stripped:
        raise ShellSecurityError("Input redirection '<' is forbidden")

    # Split into pipeline stages
    pipe_stages = _split_pipeline(stripped)
    for stage in pipe_stages:
        stage_clean = stage.strip().rstrip("\\").strip()
        if not stage_clean:
            raise ShellSecurityError("Empty stage in pipeline")

        # Strip 2>/dev/null
        stage_clean = re.sub(r"2>\s*/dev/null", "", stage_clean).strip().rstrip("\\").strip()
        if not stage_clean:
            continue

        try:
            tokens = shlex.split(stage_clean)
        except ValueError as exc:
            raise ShellSecurityError(f"Malformed shell command: {exc}") from exc

        if not tokens:
            raise ShellSecurityError("Empty stage tokens")

        binary = tokens[0]
        binary_base = os.path.basename(binary)

        if binary_base not in READ_BINARY_ALLOWLIST:
            raise ShellSecurityError(f"Disallowed binary: '{binary_base}'. Allowed: {sorted(READ_BINARY_ALLOWLIST)}")

        # find: strictly forbid -exec, -execdir, -ok, -okdir, -delete
        if binary_base == "find":
            forbidden_find_flags = {"-exec", "-execdir", "-ok", "-okdir", "-delete"}
            for tok in tokens[1:]:
                if tok in forbidden_find_flags or any(tok.startswith(f) for f in forbidden_find_flags):
                    raise ShellSecurityError(f"find flag '{tok}' is strictly forbidden")

        # awk: forbid system, popen, and redirection
        if binary_base == "awk":
            for tok in tokens[1:]:
                if re.search(r"\bsystem\b", tok):
                    raise ShellSecurityError("awk execution via 'system' is strictly forbidden")
                if re.search(r"\bpopen\b", tok):
                    raise ShellSecurityError("awk 'popen' is strictly forbidden")
                if re.search(r"(?:>|>>|<|\|)", tok):
                    raise ShellSecurityError("awk redirection is strictly forbidden")

        # sed: require -n flag and forbid "e" execution flag
        if binary_base == "sed":
            if "-n" not in tokens:
                raise ShellSecurityError("sed command must include '-n' flag for read-only operation")
            scripts = []
            idx = 1
            has_script_flag = False
            while idx < len(tokens):
                tok = tokens[idx]
                if tok in ("-f", "--file"):
                    raise ShellSecurityError("sed script file flag ('-f', '--file') is forbidden")
                if tok == "-e" and idx + 1 < len(tokens):
                    scripts.append(tokens[idx + 1])
                    idx += 2
                    has_script_flag = True
                    continue
                elif tok.startswith("--expression="):
                    scripts.append(tok.split("=", 1)[1])
                    has_script_flag = True
                idx += 1

            if not has_script_flag:
                for tok in tokens[1:]:
                    if not tok.startswith("-"):
                        scripts.append(tok)
                        break

            for script in scripts:
                if re.search(r"(?:^|[\s;\d,$/])e(?:\s|$)", script):
                    raise ShellSecurityError("sed execution command 'e' is strictly forbidden")
                if re.search(r"(?:^|[\s;\d,$/])s(.)(?:\\.|(?!\1).)*\1(?:\\.|(?!\1).)*\1[a-zA-Z]*e", script):
                    raise ShellSecurityError("sed execution flag 'e' in substitution is strictly forbidden")

        # xargs validation: ensure the sub-command is also allowlisted
        if binary_base == "xargs":
            sub_tokens = [t for t in tokens[1:] if not t.startswith("-")]
            if sub_tokens:
                sub_binary = os.path.basename(sub_tokens[0])
                if sub_binary not in READ_BINARY_ALLOWLIST:
                    raise ShellSecurityError(f"xargs invoked disallowed binary: '{sub_binary}'")

        # Check all argument tokens for path confinement
        for tok in tokens[1:]:
            _validate_path_confinement(tok, allowed_base)


def execute_incident_command(
    command: str,
    store_path: str = DEFAULT_INCIDENT_STORE,
    timeout: int = EXECUTION_TIMEOUT_SECONDS,
    stdout_limit: int = STDOUT_LIMIT_BYTES,
) -> ShellExecutionResult:
    """Validate and execute a constrained shell command."""
    validate_shell_command(command, allowed_base=store_path)

    target_dir = Path(store_path)
    target_dir.mkdir(parents=True, exist_ok=True)

    try:
        proc = subprocess.run(
            ["bash", "-c", command],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(target_dir),
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"Command timed out after {timeout} seconds: {command}") from exc

    stdout = proc.stdout
    if len(stdout.encode("utf-8")) > stdout_limit:
        stdout = stdout.encode("utf-8")[:stdout_limit].decode("utf-8", errors="ignore")

    return ShellExecutionResult(
        stdout=stdout,
        stderr=proc.stderr,
        exit_code=proc.returncode,
    )


def run_incident_search(command: str, store_path: str = DEFAULT_INCIDENT_STORE) -> str:
    """Execute a validated read-only search pipeline over incident records.
    
    Args:
        command: Pipeline conforming to binary allowlist and path constraints.
        store_path: Root incident manager store directory.
        
    Returns:
        Command stdout as string.
    """
    res = execute_incident_command(command, store_path=store_path)
    return res.stdout


# Registered alias for backwards compatibility
run_incident_shell = run_incident_search
