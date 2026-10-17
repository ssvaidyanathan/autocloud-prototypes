#!/usr/bin/env python3
"""Verify that every relative markdown link in repository .md files resolves to an existing file.

Allows relative links pointing to root files such as '../LICENSE' in monorepo subprojects.
Excludes virtual environments, caches, and git directories.
"""

import argparse
import re
import sys
from pathlib import Path
from typing import List, NamedTuple, Set, Tuple

# Markdown link extraction regex:
# 1. Inline code span: `+...`+ (ignored)
# 2. Markdown badge: [![alt](img_url)](target_url)
# 3. Markdown image: ![alt](img_url)
# 4. Standard link: [text](target_url)
LINK_PATTERN = re.compile(
    r"(`+)(?:[\s\S]*?)\1|"
    r"\[!\[([^\]]*)\]\(([^)]+)\)\]\(([^)]+)\)|"
    r"!\[([^\]]*)\]\(([^)]+)\)|"
    r"\[([^\]]+)\]\(([^)]+)\)"
)

# Allowed external/monorepo relative targets that may exist outside this subproject
ALLOWED_RELATIVE_TARGETS: Set[str] = {
    "../LICENSE",
    "../../LICENSE",
}

IGNORED_DIR_NAMES: Set[str] = {
    ".git",
    ".venv",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".terraform",
    "__pycache__",
}


class BrokenLink(NamedTuple):
    source_file: Path
    line_number: int
    link_type: str
    target: str
    resolved_path: Path


def is_ignored(path: Path) -> bool:
    """Return True if any parent directory part matches IGNORED_DIR_NAMES."""
    return any(part in IGNORED_DIR_NAMES or part.startswith(".") for part in path.parts[:-1])


def extract_links(content: str) -> List[Tuple[int, str, str]]:
    """Extract list of (line_number, link_type, target) from markdown content."""
    # Strip multi-line fenced code blocks to prevent spurious link matching
    fenced_block_pattern = re.compile(r"(```|~~~)(?:[\s\S]*?)\1")

    # Replace fenced blocks with blank lines to preserve line numbers
    def blank_out(match: re.Match[str]) -> str:
        newlines = match.group(0).count("\n")
        return "\n" * newlines

    sanitized_content = fenced_block_pattern.sub(blank_out, content)

    links: List[Tuple[int, str, str]] = []
    lines = sanitized_content.splitlines()

    for line_idx, line in enumerate(lines, start=1):
        for m in LINK_PATTERN.finditer(line):
            if m.group(1):
                # Code span: ignore
                continue
            if m.group(2) is not None:
                # Badge: check both image source and link target
                links.append((line_idx, "badge-image", m.group(3).strip()))
                links.append((line_idx, "badge-link", m.group(4).strip()))
            elif m.group(5) is not None:
                # Image
                links.append((line_idx, "image", m.group(6).strip()))
            elif m.group(7) is not None:
                # Standard link
                links.append((line_idx, "link", m.group(8).strip()))

    return links


def check_markdown_links(repo_root: Path, verbose: bool = False) -> List[BrokenLink]:
    """Scan all markdown files in repo_root and verify all relative links resolve."""
    md_files = [p for p in repo_root.rglob("*.md") if not is_ignored(p)]
    broken_links: List[BrokenLink] = []
    verified_count = 0

    for md_file in sorted(md_files):
        try:
            content = md_file.read_text(encoding="utf-8")
        except Exception as err:
            print(f"ERROR: Unable to read file {md_file}: {err}", file=sys.stderr)
            continue

        raw_links = extract_links(content)
        for line_no, link_type, target in raw_links:
            # Skip empty links, anchor-only links, mailto, and absolute web URLs
            if not target:
                continue
            prefixes = ("http://", "https://", "mailto:", "ftp://", "#")
            if any(target.startswith(prefix) for prefix in prefixes):
                continue

            # Strip fragment identifier and query parameters
            clean_target = target.split("#")[0].split("?")[0].strip()
            if not clean_target:
                # Anchors on the same page are skipped
                continue

            # Check allowed relative targets (e.g., ../LICENSE for monorepo roots)
            if clean_target in ALLOWED_RELATIVE_TARGETS:
                verified_count += 1
                if verbose:
                    print(f"[ALLOWED] {md_file.relative_to(repo_root)}:{line_no} -> {clean_target}")
                continue

            # Resolve relative link
            resolved = (md_file.parent / clean_target).resolve()
            if resolved.exists():
                verified_count += 1
                if verbose:
                    print(f"[OK] {md_file.relative_to(repo_root)}:{line_no} -> {clean_target}")
            else:
                broken_links.append(
                    BrokenLink(
                        source_file=md_file.relative_to(repo_root),
                        line_number=line_no,
                        link_type=link_type,
                        target=target,
                        resolved_path=resolved,
                    )
                )

    print(f"Scanned {len(md_files)} markdown file(s).")
    print(f"Verified {verified_count} relative link(s).")
    return broken_links


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify markdown relative links resolve to existing files."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Root directory of the repository to scan (defaults to repository root).",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print verbose link verification output.",
    )
    args = parser.parse_args()

    repo_root = args.root.resolve()
    print("========================================================")
    print(f"Checking markdown relative links in: {repo_root}")
    print("========================================================")

    broken_links = check_markdown_links(repo_root, verbose=args.verbose)

    if broken_links:
        print(f"\nFAILURE: Found {len(broken_links)} broken relative link(s):\n", file=sys.stderr)
        for b in broken_links:
            print(
                f"  - {b.source_file}:{b.line_number} [{b.link_type}] '{b.target}'"
                f"\n    Resolved path does not exist: {b.resolved_path}",
                file=sys.stderr,
            )
        return 1

    print("[PASS] All relative markdown links resolve successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
