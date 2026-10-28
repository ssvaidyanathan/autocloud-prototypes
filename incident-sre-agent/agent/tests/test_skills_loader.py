from pathlib import Path

import pytest
from agent.app.skills_loader import SkillsLoader, get_skill_toolset_for_target


def test_skills_loader_discovery_and_progressive_disclosure() -> None:
    loader = SkillsLoader()
    skills = loader.discover_skills()

    assert len(skills) >= 9
    assert "cloud-logging-queries" in skills
    assert "service-shop-api" in skills

    meta = skills["cloud-logging-queries"]
    assert meta.name == "cloud-logging-queries"
    assert meta.description
    assert "error_signatures.md" in meta.references

    # Content loaded on demand
    content = loader.get_skill_content("cloud-logging-queries")
    assert content.metadata.name == "cloud-logging-queries"
    assert "Cloud Logging Queries" in content.instructions
    assert "error_signatures.md" in content.reference_contents
    assert "Common Error Signatures Cookbook" in content.reference_contents["error_signatures.md"]


def test_skills_mapping_by_target() -> None:
    loader = SkillsLoader()
    loader.load_skills_mapping()

    # 1. Unprovided service: zero service runbooks in mapping
    dispatcher_skills = [s.name for s in loader.get_skills_for_target("dispatcher")]
    assert "triage-playbook" in dispatcher_skills
    assert "service-shop-api" not in dispatcher_skills

    logs_skills = [s.name for s in loader.get_skills_for_target("logs")]
    assert "cloud-logging-queries" in logs_skills
    assert "service-shop-api" not in logs_skills

    metrics_skills = [s.name for s in loader.get_skills_for_target("metrics")]
    assert "cloud-monitoring-metrics" in metrics_skills

    synthesizer_skills = [s.name for s in loader.get_skills_for_target("synthesizer")]
    assert "report-style" in synthesizer_skills

    # 2. Dynamic runtime resolution when service_name is provided
    dispatcher_with_svc = [
        s.name for s in loader.get_skills_for_target("dispatcher", service_name="shop-api")
    ]
    assert "service-shop-api" in dispatcher_with_svc

    logs_with_svc = [s.name for s in loader.get_skills_for_target("logs", service_name="shop-api")]
    assert "cloud-logging-queries" in logs_with_svc
    assert "service-shop-api" in logs_with_svc


def test_unknown_or_unprovided_service_gets_no_runbook_and_no_error() -> None:
    """Verify that an unknown or unprovided service gets no runbook and does not error."""
    # 1. Unprovided service (service_name=None)
    toolset_none = get_skill_toolset_for_target("logs", service_name=None)
    names_none = [s.name for s in toolset_none.skills]
    assert set(names_none) == {"cloud-logging-queries", "gke-troubleshooting"}
    assert not any(s.startswith("service-") for s in names_none)

    # 2. Empty service string (service_name="")
    toolset_empty = get_skill_toolset_for_target("logs", service_name="")
    names_empty = [s.name for s in toolset_empty.skills]
    assert set(names_empty) == {"cloud-logging-queries", "gke-troubleshooting"}
    assert not any(s.startswith("service-") for s in names_empty)

    # 3. Unknown service (service_name="unknown-svc")
    toolset_unknown = get_skill_toolset_for_target("logs", service_name="unknown-service-xyz")
    names_unknown = [s.name for s in toolset_unknown.skills]
    assert set(names_unknown) == {"cloud-logging-queries", "gke-troubleshooting"}
    assert not any(s.startswith("service-") for s in names_unknown)

    # 4. Known service gets dynamically attached
    toolset_known = get_skill_toolset_for_target("logs", service_name="shop-api")
    names_known = [s.name for s in toolset_known.skills]
    assert set(names_known) == {
        "cloud-logging-queries",
        "gke-troubleshooting",
        "service-shop-api",
    }

    # 5. SkillsLoader direct calls behave equivalently
    loader = SkillsLoader()
    skills_none = loader.get_skills_for_target("logs", service_name=None)
    assert set(s.name for s in skills_none) == {
        "cloud-logging-queries",
        "gke-troubleshooting",
    }

    skills_unknown = loader.get_skills_for_target("logs", service_name="unknown-service-xyz")
    assert set(s.name for s in skills_unknown) == {
        "cloud-logging-queries",
        "gke-troubleshooting",
    }


def test_skills_loader_rejects_scripts_directory(tmp_path: Path) -> None:
    # Security Guardrail Test: A skill containing a scripts/ directory MUST be rejected
    malicious_skill_dir = tmp_path / "bad-skill"
    malicious_skill_dir.mkdir()
    (malicious_skill_dir / "SKILL.md").write_text(
        "---\nname: bad-skill\ndescription: Attempts to include scripts\n---\n# Bad Skill",
        encoding="utf-8",
    )
    (malicious_skill_dir / "scripts").mkdir()
    (malicious_skill_dir / "scripts" / "run.sh").write_text("echo evil", encoding="utf-8")

    loader = SkillsLoader(skills_dir=tmp_path)
    with pytest.raises(ValueError, match="forbidden 'scripts/' directory"):
        loader.discover_skills()


def test_skills_loader_validation_rules(tmp_path: Path) -> None:
    loader = SkillsLoader(skills_dir=tmp_path)

    # 1. Invalid Name: Uppercase / Underscores
    d1 = tmp_path / "skill-1"
    d1.mkdir()
    (d1 / "SKILL.md").write_text(
        "---\nname: Invalid_Name\ndescription: Desc\n---\nBody",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="must be kebab-case"):
        loader.discover_skills()
    for f in d1.iterdir():
        f.unlink()
    d1.rmdir()

    # 2. Name too long (> 64 chars)
    long_name = "a" * 65
    d2 = tmp_path / "skill-2"
    d2.mkdir()
    (d2 / "SKILL.md").write_text(
        f"---\nname: {long_name}\ndescription: Desc\n---\nBody",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="exceeds maximum length of 64 characters"):
        loader.discover_skills()
    for f in d2.iterdir():
        f.unlink()
    d2.rmdir()

    # 3. Missing / empty description
    d3 = tmp_path / "skill-3"
    d3.mkdir()
    (d3 / "SKILL.md").write_text(
        "---\nname: valid-name\ndescription: ''\n---\nBody",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="missing required non-empty 'description'"):
        loader.discover_skills()
    for f in d3.iterdir():
        f.unlink()
    d3.rmdir()

    # 4. Missing frontmatter delimiters
    d4 = tmp_path / "skill-4"
    d4.mkdir()
    (d4 / "SKILL.md").write_text(
        "name: valid-name\ndescription: Desc\nBody without dashes",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="must contain YAML frontmatter"):
        loader.discover_skills()


def test_skills_loader_tracks_loaded_skills() -> None:
    """Verify SkillsLoader dynamically records loaded_skills when get_skill_content is called."""
    loader = SkillsLoader()
    assert loader.loaded_skills == []

    loader.get_skill_content("cloud-logging-queries")
    assert loader.loaded_skills == ["cloud-logging-queries"]

    loader.get_skill_content("report-style")
    assert loader.loaded_skills == ["cloud-logging-queries", "report-style"]

    # Deduplication check
    loader.get_skill_content("cloud-logging-queries")
    assert loader.loaded_skills == ["cloud-logging-queries", "report-style"]

    loader.reset_loaded_skills()
    assert loader.loaded_skills == []


def test_report_skills_used_contains_only_actually_consulted_skills() -> None:
    """Verify report.skills_used contains strictly actually consulted skills,
    and does NOT contain unconsulted skills.
    """
    from datetime import datetime, timezone

    from agent.app.contracts.findings import Findings
    from agent.app.contracts.incident import Incident
    from agent.app.synthesizer import SynthesizerAgent
    from agent.app.triage import InvestigationPlan

    loader = SkillsLoader()
    assert loader.loaded_skills == []

    agent = SynthesizerAgent(skills_loader=loader)
    incident = Incident(
        id="inc-1",
        source="generic",
        external_id="ext-1",
        external_key="INC-001",
        title="Test incident",
        description="Test description",
        severity="P2",
        opened_at=datetime.now(timezone.utc),
    )
    plan = InvestigationPlan(
        affected_service="test-service",
        symptoms=["errors"],
        selected_specialists=["logs"],
        start_time=datetime.now(timezone.utc),
        end_time=datetime.now(timezone.utc),
    )
    findings = {
        "logs": Findings(
            summary="Log findings",
            evidence=[],
            anomalies=[],
            queries_run=[],
            confidence=0.8,
            errors=[],
        )
    }

    # 1. Synthesize without passing skills_used: only report-style is consulted by synthesizer
    report1 = agent.synthesize(incident, plan, findings)
    assert report1.skills_used == ["report-style"]
    assert "triage-playbook" not in report1.skills_used
    assert "cloud-run-troubleshooting" not in report1.skills_used
    assert "cloud-logging-queries" not in report1.skills_used
    assert "cloud-monitoring-metrics" not in report1.skills_used
    assert "change-correlation" not in report1.skills_used

    # 2. Simulate triage having consulted triage-playbook
    loader.get_skill_content("triage-playbook")
    assert "triage-playbook" in loader.loaded_skills

    report2 = agent.synthesize(incident, plan, findings, skills_used=list(loader.loaded_skills))
    assert set(report2.skills_used) == {"triage-playbook", "report-style"}
    assert "cloud-run-troubleshooting" not in report2.skills_used
    assert "cloud-logging-queries" not in report2.skills_used
    assert "cloud-monitoring-metrics" not in report2.skills_used
    assert "change-correlation" not in report2.skills_used


def test_llm_generate_report_skills_used_truthfulness() -> None:
    """Verify GeminiLlmClient.generate_report populates skills_used truthfully
    without force-injecting unconsulted pipeline skills.
    """
    from datetime import datetime, timezone
    from unittest.mock import MagicMock

    from agent.app.contracts.incident import Incident
    from agent.app.contracts.report import Report
    from agent.app.llm import GeminiLlmClient
    from agent.app.triage import InvestigationPlan

    loader = SkillsLoader()
    loader.get_skill_content("triage-playbook")

    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.text = Report(
        summary="LLM Summary",
        impact="LLM Impact",
        skills_used=[],
    ).model_dump_json()
    mock_client.models.generate_content.return_value = mock_resp

    llm_client = GeminiLlmClient(client=mock_client, skills_loader=loader)
    incident = Incident(
        id="inc-1",
        source="generic",
        external_id="ext-1",
        external_key="INC-001",
        title="Test incident",
        description="Test description",
        severity="P2",
        opened_at=datetime.now(timezone.utc),
    )
    plan = InvestigationPlan(
        affected_service="test-service",
        symptoms=["errors"],
        selected_specialists=["logs"],
        start_time=datetime.now(timezone.utc),
        end_time=datetime.now(timezone.utc),
    )

    report = llm_client.generate_report(
        incident=incident,
        plan=plan,
        findings={},
        skills_used=["triage-playbook", "report-style"],
    )
    assert report.skills_used == ["triage-playbook", "report-style"]
    assert "cloud-run-troubleshooting" not in report.skills_used
    assert "cloud-logging-queries" not in report.skills_used
