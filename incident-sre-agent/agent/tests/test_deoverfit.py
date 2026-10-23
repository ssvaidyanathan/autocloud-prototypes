"""Automated CI guard tests ensuring agent/app contains zero demo overfit."""

import os
import re
from pathlib import Path


def test_agent_app_has_no_demo_overfit() -> None:
    """Verify agent/app/ (.py and prompts/*.txt) contains ZERO forbidden demo identifiers."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    agent_app_dir = repo_root / "agent" / "app"
    assert agent_app_dir.exists(), f"Directory {agent_app_dir} does not exist"

    # Forbidden patterns per developer brief
    forbidden_patterns = [
        (r"shop-api", "shop-api"),
        (r"inventory-svc", "inventory-svc"),
        (r"PAYMENT_GATEWAY_KEY", "PAYMENT_GATEWAY_KEY"),
        (r"sync-tenant-data", "sync-tenant-data"),
        (r"[Ss]cenario [0-9]", "Scenario [0-9]"),
        (r"1024\s*(?:MiB|MB)", "1024 MiB"),
        (r"1057\s*(?:MiB|MB)", "1057 MiB"),
        (r"\bcheckout\b", "checkout"),
        (r"\bcatalog\b", "catalog"),
        (r"[Dd]elay injection", "delay injection"),
    ]

    violations: list[str] = []

    target_exts = (".py", ".txt", ".yaml", ".yml")
    for root, _, files in os.walk(agent_app_dir):
        for file in files:
            if not file.endswith(target_exts):
                continue

            file_path = Path(root) / file
            rel_path = file_path.relative_to(repo_root)

            with open(file_path, "r", encoding="utf-8") as f:
                for line_no, line in enumerate(f, start=1):
                    for pattern, label in forbidden_patterns:
                        if re.search(pattern, line):
                            violations.append(
                                f"{rel_path}:{line_no} matches forbidden '{label}': {line.strip()}"
                            )

    assert not violations, "Found demo overfit in agent/app:\n" + "\n".join(violations)
