"""Declarative agent configuration loader for incident-sre-agent.

Loads and validates agent metadata, sub-agent hierarchies, output schemas, and
system instructions from agent.yaml using safe YAML loading (yaml.safe_load).
Enforces fail-fast startup: missing, unreadable, or malformed YAML raises
AgentConfigError with zero silent fallbacks.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)

DEFAULT_YAML_PATH = Path(__file__).resolve().parent / "agent.yaml"


class AgentConfigError(RuntimeError):
    """Raised when agent.yaml is missing, unreadable, or malformed."""


class AgentNodeConfig(BaseModel):
    """Configuration for an individual agent node in the ADK agent tree."""

    model_config = ConfigDict(extra="forbid")

    agent_class: str = Field(..., description="Agent class name (e.g. LlmAgent, ParallelAgent)")
    name: str = Field(..., description="Unique agent identifier")
    description: Optional[str] = Field(default=None, description="Human-readable description")
    model: Optional[str] = Field(
        default=None, description="Config or environment reference to model"
    )
    instruction: Optional[str] = Field(default=None, description="System instruction prompt")
    output_schema: Optional[str] = Field(default=None, description="Import path for output schema")
    sub_agents: Optional[List[AgentNodeConfig]] = Field(
        default=None, description="Child agent nodes for composite/parallel agents"
    )


class RootAgentConfig(BaseModel):
    """Top-level agent configuration corresponding to root_agent.yaml."""

    model_config = ConfigDict(extra="forbid")

    agent_class: str = Field(..., description="Root agent class name (BaseAgent)")
    name: str = Field(..., description="Root agent name (dispatcher)")
    description: str = Field(..., description="High-level orchestrator description")
    sub_agents: List[AgentNodeConfig] = Field(..., description="Top-level sub-agent list")

    def _build_node_index(self) -> Dict[str, AgentNodeConfig]:
        """Flatten agent tree into a name -> AgentNodeConfig mapping."""
        index: Dict[str, AgentNodeConfig] = {}

        def _traverse(node: AgentNodeConfig) -> None:
            index[node.name] = node
            if node.sub_agents:
                for child in node.sub_agents:
                    _traverse(child)

        for sub in self.sub_agents:
            _traverse(sub)
        return index

    def get_agent(self, name: str) -> AgentNodeConfig:
        """Retrieve an agent node by name or alias."""
        alias_map = {
            "changes": "changes_specialist",
            "logs": "logs_specialist",
            "metrics": "metrics_specialist",
            "resources": "resources_specialist",
        }
        resolved_name = alias_map.get(name, name)
        index = self._build_node_index()
        if resolved_name not in index:
            raise AgentConfigError(
                f"Agent node '{resolved_name}' not found in agent.yaml configuration. "
                f"Available nodes: {sorted(list(index.keys()))}"
            )
        return index[resolved_name]

    def get_instruction(self, name: str) -> str:
        """Retrieve system instruction for an agent, ensuring non-empty stripped content."""
        agent = self.get_agent(name)
        if not agent.instruction or not agent.instruction.strip():
            raise AgentConfigError(
                f"Agent '{agent.name}' has no system instruction configured in agent.yaml."
            )
        return agent.instruction.strip()

    def get_model(self, name: str) -> Optional[str]:
        """Retrieve model string with dynamic environment or config resolution."""
        agent = self.get_agent(name)
        if not agent.model:
            return None

        model_str = agent.model.strip()
        # Resolve ${VAR_NAME} or ${VAR_NAME:-default}
        env_match = re.match(r"^\$\{([A-Za-z0-9_]+)(?::-([^}]+))?\}$", model_str)
        if env_match:
            var_name = env_match.group(1)
            default_val = env_match.group(2)
            env_val = os.getenv(var_name, "").strip()
            if env_val:
                return env_val

            # Resolve from application config settings
            try:
                from .config import get_settings

                settings = get_settings()
                setting_key = var_name.lower()
                if hasattr(settings, setting_key):
                    setting_val = getattr(settings, setting_key)
                    if setting_val:
                        return str(setting_val)
            except Exception:
                pass

            if default_val is not None:
                return default_val
            return None

        return model_str

    def get_description(self, name: str) -> Optional[str]:
        """Retrieve description for an agent node or root."""
        if name in (self.name, "root", "dispatcher"):
            return self.description
        return self.get_agent(name).description


_CACHED_CONFIG: Optional[RootAgentConfig] = None
_CACHED_PATH: Optional[Path] = None


def load_agent_yaml(yaml_path: Optional[Path] = None) -> RootAgentConfig:
    """Load, parse, and strictly validate agent.yaml.

    Raises:
        AgentConfigError: if file is missing, unreadable, invalid YAML, or invalid schema.
    """
    path = yaml_path or DEFAULT_YAML_PATH
    if not path.exists():
        raise AgentConfigError(
            f"Required agent configuration file not found at: {path}. "
            "agent.yaml must exist and define all agent instructions and metadata."
        )

    try:
        content = path.read_text(encoding="utf-8")
    except Exception as exc:
        raise AgentConfigError(f"Failed to read agent configuration file at {path}: {exc}") from exc

    if not content or not content.strip():
        raise AgentConfigError(
            f"Agent configuration file at {path} is empty. "
            "A complete root_agent.yaml compatible definition is required."
        )

    try:
        raw_data: Any = yaml.safe_load(content)
    except Exception as exc:
        raise AgentConfigError(
            f"Agent configuration file at {path} contains invalid YAML: {exc}"
        ) from exc

    if not isinstance(raw_data, dict):
        raise AgentConfigError(
            f"Agent configuration in {path} must be a top-level mapping, "
            f"got {type(raw_data).__name__}."
        )

    try:
        config = RootAgentConfig.model_validate(raw_data)
    except ValidationError as val_err:
        raise AgentConfigError(
            f"Agent configuration in {path} failed schema validation:\n{val_err}"
        ) from val_err

    # Semantic validation of required agents
    required_top_level = {"triage", "specialists_parallel", "synthesizer"}
    found_top_level = {sub.name for sub in config.sub_agents}
    missing_top_level = required_top_level - found_top_level
    if missing_top_level:
        missing_sorted = sorted(list(missing_top_level))
        raise AgentConfigError(
            f"Agent configuration in {path} missing required top-level sub_agents: {missing_sorted}"
        )

    # Validate specialist parallel agents
    parallel_node = next(
        (sub for sub in config.sub_agents if sub.name == "specialists_parallel"), None
    )
    if not parallel_node or not parallel_node.sub_agents:
        raise AgentConfigError(
            f"Agent configuration in {path} must define sub_agents for 'specialists_parallel'."
        )

    required_specialists = {
        "changes_specialist",
        "logs_specialist",
        "metrics_specialist",
        "resources_specialist",
    }
    found_specialists = {sub.name for sub in parallel_node.sub_agents}
    missing_specialists = required_specialists - found_specialists
    if missing_specialists:
        missing_spec_sorted = sorted(list(missing_specialists))
        raise AgentConfigError(
            f"Agent configuration in {path} missing required specialists: {missing_spec_sorted}"
        )

    # Validate that every LLM agent has an instruction
    for node in [sub for sub in config.sub_agents if sub.agent_class == "LlmAgent"]:
        if not node.instruction or not node.instruction.strip():
            raise AgentConfigError(
                f"Agent configuration error: agent '{node.name}' has no system instruction."
            )

    for spec in parallel_node.sub_agents:
        if not spec.instruction or not spec.instruction.strip():
            raise AgentConfigError(
                f"Agent configuration error: specialist '{spec.name}' has no system instruction."
            )

    return config


def get_agent_yaml_config(
    yaml_path: Optional[Path] = None, force_reload: bool = False
) -> RootAgentConfig:
    """Retrieve cached RootAgentConfig singleton or load from disk."""
    global _CACHED_CONFIG, _CACHED_PATH
    target_path = yaml_path or DEFAULT_YAML_PATH
    if _CACHED_CONFIG is None or force_reload or _CACHED_PATH != target_path:
        _CACHED_CONFIG = load_agent_yaml(target_path)
        _CACHED_PATH = target_path
    return _CACHED_CONFIG


def clear_agent_yaml_cache() -> None:
    """Clear cached configuration for test isolation."""
    global _CACHED_CONFIG, _CACHED_PATH
    _CACHED_CONFIG = None
    _CACHED_PATH = None


__all__ = [
    "AgentConfigError",
    "AgentNodeConfig",
    "RootAgentConfig",
    "clear_agent_yaml_cache",
    "get_agent_yaml_config",
    "load_agent_yaml",
]
