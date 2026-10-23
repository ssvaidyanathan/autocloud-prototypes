"""Negative test fixture file containing deliberate inline instruction and prompt violations.

Used by test_agent_config.py to verify that check-no-inline-prompts.py catches
every category of hardcoded instruction or prompt in Python code.
"""

from typing import Any


class Part:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


def sample_violating_function(agent: Any, incident: Any) -> None:
    # 1. Hardcoded instruction keyword argument
    _ = agent(instruction="You are a hardcoded agent.")

    # 2. Hardcoded prompt passed to _execute_adk_runner
    _ = agent._execute_adk_runner(agent, f"Investigate failure for incident {incident.id}")

    # 3. Variable assignment to prompt_lines with prompt phrases
    prompt_lines = [
        f"Investigate pipeline failure for incident {incident.id}",
        "CRITICAL INSTRUCTIONS:",
        "1. Turn 1: Invoke load_skill exactly once.",
    ]
    _ = prompt_lines

    # 4. Dict key assignment with instruction
    config_dict: dict[str, Any] = {}
    config_dict["instruction"] = "Hardcoded instruction in dict"

    # 5. Part(text=...) prompt call
    _ = Part(text=f"Hardcoded Part prompt for {incident}")
