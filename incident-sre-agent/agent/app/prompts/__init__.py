from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parent


def load_prompt(name: str) -> str:
    """Load prompt text file from agent/app/prompts/<name>.txt."""
    prompt_file = _PROMPTS_DIR / f"{name}.txt"
    if prompt_file.exists():
        return prompt_file.read_text(encoding="utf-8").strip()
    return ""


__all__ = ["load_prompt"]
