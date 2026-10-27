"""auto_close of the ata-* alert policies comes from a validated Terraform variable.

It was the literal "1800s" in each of the 8 policies (infra/terraform/observability/main.tf).
Cloud Monitoring requires at least 30 minutes. The default keeps the live value: a read-only
plan against the live state shows no changes.
"""

import re
from pathlib import Path

OBS = Path(__file__).resolve().parents[1] / "infra" / "terraform" / "observability"


def _variable_block(name: str) -> str:
    text = (OBS / "variables.tf").read_text()
    start = text.index(f'variable "{name}"')
    nxt = text.find("\nvariable ", start + 1)
    return text[start:] if nxt == -1 else text[start:nxt]


def test_every_policy_takes_auto_close_from_the_variable():
    main_tf = (OBS / "main.tf").read_text()
    policies = re.findall(r'resource "google_monitoring_alert_policy" "(\w+)"', main_tf)
    assert len(policies) == 8
    assert main_tf.count('auto_close = "${var.alert_auto_close_secs}s"') == len(policies)
    assert not re.search(r'auto_close\s*=\s*"\d+s"', main_tf)


def test_auto_close_variable_default_and_validation():
    block = _variable_block("alert_auto_close_secs")
    assert "type        = number" in block
    assert re.search(r"default\s*=\s*1800\b", block)
    assert "validation {" in block
    assert "var.alert_auto_close_secs >= 1800" in block
    assert "floor(var.alert_auto_close_secs) == var.alert_auto_close_secs" in block
