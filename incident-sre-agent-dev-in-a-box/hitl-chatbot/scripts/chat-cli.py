#!/usr/bin/env python3
"""CLI entrypoint runner for SRE HITL Chatbot."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Add project root to sys.path
script_dir = Path(__file__).resolve().parent
project_root = script_dir.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from app.adapters.cli import CliAdapter  # noqa: E402
from app.agent import ChatbotAgentRunner  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="SRE HITL Chatbot CLI Runner",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--case",
        choices=["A", "B", "interactive"],
        default="interactive",
        help="Use case to execute: 'A' (rollout query), 'B' (alert Jira filing), or 'interactive'",
    )
    parser.add_argument(
        "--project",
        default=os.environ.get("PROJECT_ID", "default-project"),
        help="Target project identifier",
    )
    parser.add_argument(
        "--query",
        default=None,
        help="Custom user query to run",
    )
    parser.add_argument(
        "--auto-confirm",
        action="store_true",
        help="Automatically approve HITL confirmation gates (for automated tests / headless CI)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    runner = ChatbotAgentRunner()
    adapter = CliAdapter(runner=runner)

    print("========================================================")
    print("SRE Interactive Chatbot & HITL CLI Runner")
    print("========================================================")

    if args.query:
        query = args.query
        adapter.run_query(query, auto_confirm=args.auto_confirm)
        return 0

    if args.case == "A":
        query = "what changed in the latest rollout?"
        adapter.run_query(query, auto_confirm=args.auto_confirm)
        return 0

    if args.case == "B":
        query = (
            f"file a JIRA ticket in response to the latest ip-health-snow alert "
            f"in {args.project} project"
        )
        adapter.run_query(query, auto_confirm=args.auto_confirm)
        return 0

    # Interactive loop
    print("Interactive Chat Mode. Type 'exit' or 'quit' to terminate.")
    print("Examples:")
    print("  Case A: 'what changed in the latest rollout?'")
    print(
        f"  Case B: 'file a JIRA ticket in response to the latest ip-health-snow alert "
        f"in {args.project} project'"
    )
    print("--------------------------------------------------------")

    while True:
        try:
            user_input = input("\n[You] > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting chat session.")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            print("Session ended.")
            break

        adapter.run_query(user_input, auto_confirm=args.auto_confirm)

    return 0


if __name__ == "__main__":
    sys.exit(main())
