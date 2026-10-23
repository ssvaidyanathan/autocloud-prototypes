"""Declarative agent configuration loader for GDI.

Loads, validates, and indexes agent metadata, system instructions, and prompt templates
from agent.yaml (ADK Hybrid model).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Union

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, Field, model_validator

DEFAULT_AGENT_YAML_PATH = Path(__file__).parent / "agent.yaml"
_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand_env_vars(raw_text: str) -> str:
    """Expand ${VAR} and ${VAR:-default} patterns using os.environ, failing if unset."""

    def _replace(match: re.Match[str]) -> str:
        var_name = match.group(1)
        has_default = match.group(2) is not None
        default_val = match.group(2) if has_default else None

        if var_name in os.environ and os.environ[var_name].strip():
            return os.environ[var_name].strip()

        if default_val is not None and default_val.strip():
            return default_val.strip()

        raise ValueError(
            f"Referenced environment variable '{var_name}' is unset or empty."
        )

    return _ENV_VAR_PATTERN.sub(_replace, raw_text)


class SubAgentConfig(BaseModel):
    """Configuration for an individual sub-agent or specialist."""

    agent_class: str
    name: str
    description: Optional[str] = None
    model: Optional[str] = None
    instruction: Optional[str] = None
    drilldown_instruction: Optional[str] = None
    output_schema: Optional[str] = None
    sub_agents: Optional[List[SubAgentConfig]] = None


class AgentYamlConfig(BaseModel):
    """Top-level agent.yaml configuration model."""

    agent_class: str
    name: str
    description: str
    sub_agents: List[SubAgentConfig]
    prompt_templates: Dict[str, str] = Field(default_factory=dict)

    _agents_by_name: Dict[str, SubAgentConfig] = {}

    @model_validator(mode="after")
    def _index_and_validate(self) -> "AgentYamlConfig":
        """Index all sub-agents recursively and enforce required keys."""
        indexed: Dict[str, SubAgentConfig] = {}

        def _traverse(sub: SubAgentConfig) -> None:
            if not sub.name or not sub.name.strip():
                raise ValueError("Sub-agent entry must have a non-empty 'name'")
            indexed[sub.name] = sub
            if sub.sub_agents:
                for child in sub.sub_agents:
                    _traverse(child)

        for top_sub in self.sub_agents:
            _traverse(top_sub)

        object.__setattr__(self, "_agents_by_name", indexed)

        # Enforce required dispatcher metadata
        if self.name != "dispatcher":
            raise ValueError(f"Root agent name must be 'dispatcher', got '{self.name}'")
        if not self.description or not self.description.strip():
            raise ValueError("Root dispatcher description must be a non-empty string")

        # Enforce required agents and instructions
        required_instructions = ["triage", "synthesizer"]
        for agent_name in required_instructions:
            if agent_name not in indexed:
                raise ValueError(f"Missing required agent '{agent_name}' in sub_agents")
            inst = indexed[agent_name].instruction
            if not inst or not inst.strip():
                raise ValueError(
                    f"Agent '{agent_name}' must have a non-empty 'instruction'"
                )

        required_specialists = [
            "pipeline_logs_specialist",
            "runtime_health_specialist",
            "iac_config_specialist",
        ]
        for spec_name in required_specialists:
            if spec_name not in indexed:
                raise ValueError(f"Missing required specialist '{spec_name}' in sub_agents")
            spec = indexed[spec_name]
            if not spec.instruction or not spec.instruction.strip():
                raise ValueError(
                    f"Specialist '{spec_name}' must have a non-empty 'instruction'"
                )
            if not spec.drilldown_instruction or not spec.drilldown_instruction.strip():
                raise ValueError(
                    f"Specialist '{spec_name}' must have a non-empty 'drilldown_instruction'"
                )

        # Enforce required prompt templates
        required_templates = [
            "sre_synthesis",
            "pipeline_logs_runner",
            "runtime_health_runner",
            "iac_config_runner",
        ]
        for tmpl_name in required_templates:
            if tmpl_name not in self.prompt_templates:
                raise ValueError(f"Missing required prompt template '{tmpl_name}'")
            if not self.prompt_templates[tmpl_name].strip():
                raise ValueError(f"Prompt template '{tmpl_name}' must be a non-empty string")

        return self

    def get_agent(self, name: str) -> SubAgentConfig:
        """Retrieve a sub-agent configuration by name."""
        if name not in self._agents_by_name:
            raise KeyError(f"Agent '{name}' not found in agent configuration")
        return self._agents_by_name[name]

    def get_instruction(self, name: str) -> str:
        """Retrieve the primary system instruction for a named agent."""
        agent = self.get_agent(name)
        if not agent.instruction:
            raise ValueError(f"Agent '{name}' does not define an instruction")
        return agent.instruction

    def get_drilldown_instruction(self, name: str) -> str:
        """Retrieve the multi-turn drill-down instruction for a specialist."""
        agent = self.get_agent(name)
        if not agent.drilldown_instruction:
            raise ValueError(
                f"Specialist '{name}' does not define a drilldown_instruction"
            )
        return agent.drilldown_instruction

    def get_description(self, name: str) -> str:
        """Retrieve description for an agent (or root dispatcher)."""
        if name == "dispatcher" or name == self.name:
            return self.description
        agent = self.get_agent(name)
        return agent.description or ""

    def get_prompt_template(self, template_name: str) -> str:
        """Retrieve a prompt template by name."""
        if template_name not in self.prompt_templates:
            raise KeyError(f"Prompt template '{template_name}' not found")
        val = self.prompt_templates[template_name]
        if not val or not val.strip():
            raise ValueError(f"Prompt template '{template_name}' is empty")
        return val


_cached_config: Optional[AgentYamlConfig] = None
_cached_path: Optional[Path] = None


def load_agent_config(
    yaml_path: Optional[Union[str, Path]] = None,
    reload: bool = False,
) -> AgentYamlConfig:
    """Load, validate, and return the agent configuration from agent.yaml.

    Raises:
        FileNotFoundError: If the YAML file does not exist.
        ValueError: If the file is empty, malformed, or missing required keys.
    """
    global _cached_config, _cached_path

    if yaml_path is not None:
        target_path = Path(yaml_path)
    else:
        env_path = os.getenv("AGENT_YAML_PATH")
        target_path = Path(env_path) if env_path else DEFAULT_AGENT_YAML_PATH

    if not reload and _cached_config is not None and _cached_path == target_path:
        return _cached_config

    if not target_path.exists() or not target_path.is_file():
        raise FileNotFoundError(
            f"agent.yaml not found at '{target_path}'. Missing configuration is not permitted."
        )

    raw_content = target_path.read_text(encoding="utf-8")
    if not raw_content or not raw_content.strip():
        raise ValueError(f"agent.yaml at '{target_path}' is empty.")

    expanded = _expand_env_vars(raw_content)

    try:
        data = yaml.safe_load(expanded)
    except Exception as exc:
        raise ValueError(f"Malformed YAML in '{target_path}': {exc}") from exc

    if not isinstance(data, dict):
        got_type = type(data).__name__
        raise ValueError(
            f"Malformed agent.yaml at '{target_path}': expected mapping at root, got {got_type}"
        )

    try:
        config = AgentYamlConfig.model_validate(data)
    except Exception as exc:
        raise ValueError(
            f"agent.yaml schema validation failed for '{target_path}': {exc}"
        ) from exc

    _cached_config = config
    _cached_path = target_path
    return config


def get_agent_config(
    yaml_path: Optional[Union[str, Path]] = None,
    reload: bool = False,
) -> AgentYamlConfig:
    """Get the cached agent configuration, loading it if necessary."""
    return load_agent_config(yaml_path=yaml_path, reload=reload)


def reset_agent_config() -> None:
    """Reset the module-level configuration cache (primarily for tests)."""
    global _cached_config, _cached_path
    _cached_config = None
    _cached_path = None
