#!/usr/bin/env bash
set -euo pipefail

# Guard script to ensure all shell scripts and workflow run: blocks use portable
# in-place sed editing (compatible with both macOS BSD sed and Linux GNU sed).
#
# Rule: Any in-place sed invocation must use the approved sedi_args array pattern:
#   sedi_args=("-i")
#   if [[ "$(uname)" == "Darwin" ]]; then
#     sedi_args=("-i" "")
#   fi
#   sed "${sedi_args[@]}" "s/OLD/NEW/g" file
#
# Bare sed -i, sed -i '', sed -i.bak, sed --in-place, and bundled flags like -Ei
# are forbidden. Read-only sed calls (e.g. sed '$d', sed -n) are permitted.
# Files using sed "${sedi_args[@]}" must also define the Darwin check.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

python3 - "$REPO_ROOT" "$@" << 'EOF'
import os
import re
import subprocess
import sys
from pathlib import Path

repo_root = Path(sys.argv[1]).resolve()
explicit_targets = [Path(p).resolve() for p in sys.argv[2:] if p]

# Pattern matching non-portable in-place flags:
# - Bare or suffixed -i (e.g. -i, -i'', -i"", -i.bak)
# - Long flag --in-place (or --in-place=...)
# - Bundled flags containing 'i' (e.g. -Ei, -ie, -in, -iE, -ri)
IN_PLACE_FLAG = re.compile(
    r"""(?<!\w)(?:--in-place(?:=\S*)?|-i\S*|-[a-zA-Z]*i[a-zA-Z0-9_.]*)"""
)

# Pattern checking for Darwin uname branch in sedi_args definition
DARWIN_CHECK_PATTERN = re.compile(
    r"""sedi_args\s*=\s*\(\s*["']-i["']\s*\)"""
    r""".*?uname.*?Darwin"""
    r""".*?sedi_args\s*=\s*\(\s*["']-i["']\s+["']["']\s*\)""",
    re.DOTALL
)

def check_shell_line(line: str) -> list[str]:
    # Strip trailing comments (but preserve quoted #)
    # Simple comment stripping: if '#' is preceded by whitespace or at start
    code = re.sub(r'(^|\s+)#.*$', '', line).strip()
    if not code or "sed" not in code:
        return []

    errors = []
    # Match sed invocations across command separators (; | && ||)
    for m in re.finditer(r'(?:^|[;|&]\s*)\b(sed\s+[^;|&]+)', code):
        cmd = m.group(1).strip()

        # If it uses the approved form "${sedi_args[@]}"
        if "${sedi_args[@]}" in cmd:
            remainder = cmd.replace('"${sedi_args[@]}"', '').replace('${sedi_args[@]}', '')
            flag_match = IN_PLACE_FLAG.search(remainder)
            if flag_match:
                errors.append(f"Forbidden extra in-place flag '{flag_match.group(0)}' in '{cmd}'")
            continue

        # If not using sedi_args, check if an in-place flag is present
        flag_match = IN_PLACE_FLAG.search(cmd)
        if flag_match:
            errors.append(
                f"Non-portable in-place sed invocation '{flag_match.group(0)}' in '{cmd}'. "
                f"Use 'sed \"${{sedi_args[@]}}\"' with uname Darwin check instead."
            )

    return errors

def scan_sh_file(file_path: Path) -> list[tuple[str, int, str]]:
    issues = []
    try:
        content = file_path.read_text(encoding='utf-8', errors='replace')
    except Exception as e:
        return [(str(file_path), 0, f"Failed to read file: {e}")]

    lines = content.splitlines()
    uses_sedi_args = False

    for idx, line in enumerate(lines, 1):
        line_errors = check_shell_line(line)
        for err in line_errors:
            issues.append((str(file_path), idx, err))
        if "${sedi_args[@]}" in line:
            uses_sedi_args = True

    # If file uses sedi_args, verify that it defines sedi_args with the Darwin check
    if uses_sedi_args:
        if not DARWIN_CHECK_PATTERN.search(content):
            issues.append((
                str(file_path),
                1,
                "File uses '${sedi_args[@]}' but does not define sedi_args with required Darwin branch:\n"
                "    sedi_args=(\"-i\")\n"
                "    if [[ \"$(uname)\" == \"Darwin\" ]]; then\n"
                "      sedi_args=(\"-i\" \"\")\n"
                "    fi"
            ))

    return issues

def scan_yaml_workflow(file_path: Path) -> list[tuple[str, int, str]]:
    issues = []
    try:
        content = file_path.read_text(encoding='utf-8', errors='replace')
    except Exception as e:
        return [(str(file_path), 0, f"Failed to read file: {e}")]

    lines = content.splitlines()
    in_run_block = False
    run_indent = 0
    run_block_lines = []
    run_block_start = 0

    def flush_run_block():
        nonlocal in_run_block, run_block_lines, run_block_start
        if not run_block_lines:
            in_run_block = False
            return
        block_text = "\n".join(l for _, l in run_block_lines)
        uses_sedi_args = False
        for line_no, line in run_block_lines:
            line_errors = check_shell_line(line)
            for err in line_errors:
                issues.append((str(file_path), line_no, err))
            if "${sedi_args[@]}" in line:
                uses_sedi_args = True
        if uses_sedi_args and not DARWIN_CHECK_PATTERN.search(block_text):
            issues.append((
                str(file_path),
                run_block_start,
                "Workflow run: block uses '${sedi_args[@]}' without required Darwin branch definition."
            ))
        in_run_block = False
        run_block_lines = []

    for idx, line in enumerate(lines, 1):
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())

        # Check for start of run: step
        run_match = re.match(r'^(\s*)-\s*run:\s*(.*)$|^(\s*)run:\s*(.*)$', line)
        if run_match:
            flush_run_block()
            inline_cmd = run_match.group(2) if run_match.group(2) is not None else run_match.group(4)
            inline_cmd = inline_cmd.strip()
            if inline_cmd and inline_cmd not in ('|', '>', '|-', '>+', '|+', '>-'):
                # Single-line run: command
                line_errors = check_shell_line(inline_cmd)
                for err in line_errors:
                    issues.append((str(file_path), idx, err))
                if "${sedi_args[@]}" in inline_cmd:
                    issues.append((
                        str(file_path),
                        idx,
                        "Single-line run: step uses '${sedi_args[@]}' without required Darwin branch definition."
                    ))
            else:
                # Multi-line run block
                in_run_block = True
                run_indent = indent
                run_block_start = idx
                run_block_lines = []
            continue

        if in_run_block:
            if not stripped:
                run_block_lines.append((idx, line))
                continue
            if indent > run_indent:
                run_block_lines.append((idx, line))
            else:
                flush_run_block()

    flush_run_block()
    return issues

def get_target_files() -> list[Path]:
    if explicit_targets:
        return explicit_targets

    # Scan tracked *.sh files via git if possible
    files = []
    try:
        git_sh = subprocess.check_output(
            ["git", "-C", str(repo_root), "ls-files", "*.sh"],
            text=True
        ).splitlines()
        for p in git_sh:
            full = repo_root / p
            if full.is_file():
                files.append(full)
    except Exception:
        files.extend(repo_root.glob("**/*.sh"))

    # Scan workflow files
    workflows_dir = repo_root / ".github" / "workflows"
    if workflows_dir.is_dir():
        for yml in sorted(workflows_dir.glob("*.yml")) + sorted(workflows_dir.glob("*.yaml")):
            files.append(yml)

    return sorted(set(files))

targets = get_target_files()
all_issues = []

for target in targets:
    if target.suffix in ('.yml', '.yaml'):
        all_issues.extend(scan_yaml_workflow(target))
    elif target.suffix == '.sh' or target.is_file():
        all_issues.extend(scan_sh_file(target))

if all_issues:
    print(f"ERROR: Found {len(all_issues)} portable sed violation(s):\n", file=sys.stderr)
    for file_str, line_no, desc in all_issues:
        rel = os.path.relpath(file_str, repo_root) if file_str.startswith(str(repo_root)) else file_str
        print(f"{rel}:{line_no}: {desc}", file=sys.stderr)
    sys.exit(1)

print(f"SUCCESS: All {len(targets)} scanned file(s) satisfy portable sed in-place requirements.")
sys.exit(0)
EOF
