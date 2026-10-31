"""Prompt loading utilities.

Legacy loader backward-compatibility shim. System instructions and prompt
templates are externalized into agent/app/agent.yaml.
"""

from ..agent_config import get_agent_yaml_config


def load_prompt(name: str) -> str:
    """Load prompt/instruction from agent.yaml by agent name or alias.

    Args:
        name: Agent name or alias ('triage', 'synthesizer', 'changes', 'logs',
              'metrics', 'resources', 'changes_specialist', etc.)

    Returns:
        The exact stripped instruction string from agent.yaml.

    Raises:
        AgentConfigError: if agent.yaml is missing, malformed, or the instruction key is absent.
    """
    config = get_agent_yaml_config()
    return config.get_instruction(name)


__all__ = ["load_prompt"]
