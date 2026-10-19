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
from __future__ import annotations
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

def join_line_continuations(content: str, initial_ctl: list[int] | None = None) -> tuple[str, list[int]]:
    """Join line continuations (\\-newline) while tracking original 1-based line numbers."""
    char_to_line = []
    joined_chars = []
    curr_line = 1
    i = 0
    n = len(content)
    while i < n:
        if content[i] == '\\' and i + 1 < n and (content[i+1] == '\n' or (content[i+1] == '\r' and i + 2 < n and content[i+2] == '\n')):
            if content[i+1] == '\r':
                i += 3
            else:
                i += 2
            curr_line += 1
            joined_chars.append(' ')
            line_no = initial_ctl[i-1] if initial_ctl else curr_line - 1
            char_to_line.append(line_no)
        else:
            line_no = initial_ctl[i] if initial_ctl else curr_line
            joined_chars.append(content[i])
            char_to_line.append(line_no)
            if content[i] == '\n':
                curr_line += 1
            i += 1
    return "".join(joined_chars), char_to_line

def blank_comments_and_extract_substitutions(text: str, char_to_line: list[int]):
    """Blank out comments and extract command substitutions $(...) and `...`."""
    chars = list(text)
    n = len(chars)
    i = 0
    in_single = False
    in_double = False
    escaped = False
    substitutions = []

    while i < n:
        c = chars[i]
        if escaped:
            escaped = False
            i += 1
            continue

        if c == '\\' and not in_single:
            escaped = True
            i += 1
            continue

        if c == "'" and not in_double:
            in_single = not in_single
            i += 1
            continue

        if c == '"' and not in_single:
            in_double = not in_double
            i += 1
            continue

        if not in_single:
            # Check for comments (only outside quotes and at token boundary)
            if not in_double and c == '#':
                prev = chars[i-1] if i > 0 else '\n'
                if prev in ' \t\n\r;|&(<>':
                    while i < n and chars[i] != '\n':
                        chars[i] = ' '
                        i += 1
                    continue

            # Check for $(...) command substitution
            if c == '$' and i + 1 < n and chars[i+1] == '(' and (i + 2 >= n or chars[i+2] != '('):
                start_inner = i + 2
                depth = 1
                j = start_inner
                inner_escaped = False
                inner_squote = False
                inner_dquote = False
                while j < n and depth > 0:
                    cj = chars[j]
                    if inner_escaped:
                        inner_escaped = False
                        j += 1
                        continue
                    if cj == '\\' and not inner_squote:
                        inner_escaped = True
                        j += 1
                        continue
                    if cj == "'" and not inner_dquote:
                        inner_squote = not inner_squote
                        j += 1
                        continue
                    if cj == '"' and not inner_squote:
                        inner_dquote = not inner_dquote
                        j += 1
                        continue
                    if not inner_squote and not inner_dquote:
                        if cj == '(':
                            depth += 1
                        elif cj == ')':
                            depth -= 1
                            if depth == 0:
                                break
                    j += 1

                sub_text = text[start_inner:j]
                sub_ctl = char_to_line[start_inner:j]
                substitutions.append((sub_text, sub_ctl))
                for k in range(i, min(j + 1, n)):
                    chars[k] = ' '
                i = j + 1
                continue

            # Check for `...` command substitution
            if c == '`':
                start_inner = i + 1
                j = start_inner
                inner_escaped = False
                while j < n:
                    if inner_escaped:
                        inner_escaped = False
                        j += 1
                        continue
                    if chars[j] == '\\':
                        inner_escaped = True
                        j += 1
                        continue
                    if chars[j] == '`':
                        break
                    j += 1

                sub_text = text[start_inner:j]
                sub_ctl = char_to_line[start_inner:j]
                substitutions.append((sub_text, sub_ctl))
                for k in range(i, min(j + 1, n)):
                    chars[k] = ' '
                i = j + 1
                continue

        i += 1

    return "".join(chars), substitutions

def tokenize_words(cmd_str: str) -> list[str]:
    """Tokenize a shell command string into words respecting quotes."""
    tokens = []
    curr = []
    in_s = False
    in_d = False
    esc = False
    for c in cmd_str:
        if esc:
            curr.append(c)
            esc = False
            continue
        if c == '\\' and not in_s:
            esc = True
            curr.append(c)
            continue
        if c == "'" and not in_d:
            in_s = not in_s
            curr.append(c)
            continue
        if c == '"' and not in_s:
            in_d = not in_d
            curr.append(c)
            continue
        if not in_s and not in_d and c in ' \t':
            if curr:
                tokens.append("".join(curr))
                curr = []
            continue
        curr.append(c)
    if curr:
        tokens.append("".join(curr))
    return tokens

# Wrappers that execute a following command
WRAPPERS_WITH_ARGS = {
    'xargs': {'-a', '-d', '-E', '-e', '-I', '-i', '-L', '-l', '-n', '-P', '-s'},
    'sudo': {'-u', '-g', '-p', '-h', '-r', '-t', '-T', '-C'},
    'env': {'-u'},
    'command': set(),
    'exec': {'-a'},
}

def unwrap_command(tokens: list[str]) -> tuple[str | None, int]:
    """Find the target command in tokens after skipping wrappers, assignments, and !."""
    idx = 0
    n = len(tokens)
    while idx < n:
        tok = tokens[idx]
        if tok == '!':
            idx += 1
            continue
        if re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', tok):
            idx += 1
            continue

        wrapper_name = tok.split('/')[-1]
        if wrapper_name in WRAPPERS_WITH_ARGS:
            flags_with_args = WRAPPERS_WITH_ARGS[wrapper_name]
            idx += 1
            while idx < n:
                w_tok = tokens[idx]
                if w_tok == '--':
                    idx += 1
                    break
                if wrapper_name == 'env' and re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', w_tok):
                    idx += 1
                    continue
                if w_tok.startswith('-'):
                    flag_prefix = w_tok[:2]
                    if w_tok in flags_with_args or flag_prefix in flags_with_args:
                        if w_tok in flags_with_args:
                            idx += 2
                        else:
                            idx += 1
                    else:
                        idx += 1
                else:
                    break
            continue

        return tok, idx
    return None, idx

def split_into_commands(text: str, char_to_line: list[int]) -> list[tuple[str, int]]:
    """Split shell code into individual (cmd_str, start_line) tuples."""
    commands = []
    n = len(text)
    i = 0
    cmd_start_pos = None
    curr_chars = []
    in_s = False
    in_d = False
    esc = False

    DELIM_CHARS = {';', '\n', '&', '|', '(', ')'}
    CONTROL_KEYWORDS = {'if', 'then', 'elif', 'else', 'fi', 'while', 'until', 'do', 'done', 'time'}

    def flush_cmd():
        nonlocal cmd_start_pos, curr_chars
        if curr_chars and cmd_start_pos is not None:
            cmd_text = "".join(curr_chars).strip()
            if cmd_text:
                line_no = char_to_line[cmd_start_pos]
                commands.append((cmd_text, line_no))
        curr_chars = []
        cmd_start_pos = None

    while i < n:
        c = text[i]
        if esc:
            if cmd_start_pos is None:
                cmd_start_pos = i - 1
            curr_chars.append(c)
            esc = False
            i += 1
            continue

        if c == '\\' and not in_s:
            esc = True
            if cmd_start_pos is None:
                cmd_start_pos = i
            curr_chars.append(c)
            i += 1
            continue

        if c == "'" and not in_d:
            in_s = not in_s
            if cmd_start_pos is None:
                cmd_start_pos = i
            curr_chars.append(c)
            i += 1
            continue

        if c == '"' and not in_s:
            in_d = not in_d
            if cmd_start_pos is None:
                cmd_start_pos = i
            curr_chars.append(c)
            i += 1
            continue

        if not in_s and not in_d:
            if c in DELIM_CHARS:
                flush_cmd()
                i += 1
                continue

            if c.isalnum() or c == '_':
                prev = text[i-1] if i > 0 else ' '
                if prev in ' \t\n\r;|&()':
                    m = re.match(r'^[a-zA-Z_]\w*', text[i:])
                    if m and m.group(0) in CONTROL_KEYWORDS:
                        flush_cmd()
                        i += len(m.group(0))
                        continue

            if c not in ' \t':
                if cmd_start_pos is None:
                    cmd_start_pos = i
            curr_chars.append(c)
            i += 1
            continue

        curr_chars.append(c)
        i += 1

    flush_cmd()
    return commands

def scan_text(content: str, initial_ctl: list[int] | None = None) -> tuple[list[tuple[int, str]], bool]:
    """Scan shell content and return (errors, uses_sedi_args)."""
    joined, char_to_line = join_line_continuations(content, initial_ctl)
    cleaned, substitutions = blank_comments_and_extract_substitutions(joined, char_to_line)

    snippets = [(cleaned, char_to_line)]
    for sub_text, sub_ctl in substitutions:
        sub_cleaned, nested_subs = blank_comments_and_extract_substitutions(sub_text, sub_ctl)
        snippets.append((sub_cleaned, sub_ctl))
        for n_text, n_ctl in nested_subs:
            snippets.append((n_text, n_ctl))

    all_cmds = []
    for text, ctl in snippets:
        all_cmds.extend(split_into_commands(text, ctl))

    errors = []
    uses_sedi_args = False

    for cmd_str, line_no in all_cmds:
        tokens = tokenize_words(cmd_str)
        if not tokens:
            continue

        target_cmd, _ = unwrap_command(tokens)
        if not target_cmd:
            continue

        # Match sed, gsed, /path/to/sed, or /path/to/gsed
        if not re.match(r'^(?:.*\/)?g?sed$', target_cmd):
            continue

        # Check for approved sedi_args pattern
        if "${sedi_args[@]}" in cmd_str:
            uses_sedi_args = True
            rem = cmd_str.replace('"${sedi_args[@]}"', '').replace('${sedi_args[@]}', '')
            unquoted = re.sub(r"'[^']*'|\"[^\"]*\"", "''", rem)
            m = IN_PLACE_FLAG.search(unquoted)
            if m:
                errors.append((line_no, f"Forbidden extra in-place flag '{m.group(0)}' in '{cmd_str}'"))
            continue

        # Non-portable check: strip quoted strings first so s/-i/x/ is not flagged
        unquoted = re.sub(r"'[^']*'|\"[^\"]*\"", "''", cmd_str)
        m = IN_PLACE_FLAG.search(unquoted)
        if m:
            errors.append((
                line_no,
                f"Non-portable in-place sed invocation '{m.group(0)}' in '{cmd_str}'. "
                f"Use 'sed \"${{sedi_args[@]}}\"' with uname Darwin check instead."
            ))

    return errors, uses_sedi_args

def scan_sh_file(file_path: Path) -> list[tuple[str, int, str]]:
    issues = []
    try:
        content = file_path.read_text(encoding='utf-8', errors='replace')
    except Exception as e:
        return [(str(file_path), 0, f"Failed to read file: {e}")]

    errors, uses_sedi_args = scan_text(content)
    for line_no, err in errors:
        issues.append((str(file_path), line_no, err))

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

        block_chars = []
        block_ctl = []
        for line_no, line_text in run_block_lines:
            for ch in line_text:
                block_chars.append(ch)
                block_ctl.append(line_no)
            block_chars.append('\n')
            block_ctl.append(line_no)

        block_text = "".join(block_chars)
        errors, uses_sedi_args = scan_text(block_text, block_ctl)
        for line_no, err in errors:
            issues.append((str(file_path), line_no, err))

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
                errors, uses_sedi_args = scan_text(inline_cmd, [idx] * len(inline_cmd))
                for _, err in errors:
                    issues.append((str(file_path), idx, err))
                if uses_sedi_args and not DARWIN_CHECK_PATTERN.search(inline_cmd):
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

    # Scan tracked *.sh files via git if possible, excluding test fixtures
    files = []
    try:
        git_sh = subprocess.check_output(
            ["git", "-C", str(repo_root), "ls-files", "*.sh"],
            stderr=subprocess.DEVNULL,
            text=True
        ).splitlines()
        for p in git_sh:
            if "fixtures" in Path(p).parts or "fixtures/" in p:
                continue
            full = repo_root / p
            if full.is_file():
                files.append(full)
    except Exception:
        for p in repo_root.glob("**/*.sh"):
            if "fixtures" in p.parts:
                continue
            files.append(p)

    # Scan workflow files
    workflows_dir = repo_root / ".github" / "workflows"
    if workflows_dir.is_dir():
        for yml in sorted(workflows_dir.glob("*.yml")) + sorted(workflows_dir.glob("*.yaml")):
            if "fixtures" in yml.parts:
                continue
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
