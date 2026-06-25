from pathlib import Path

_PROMPT_DIR = Path(__file__).parent.parent / "prompts"

_PROMPT_FILES: dict[str, str] = {
    "parser": "parser.txt",
    "model_builder": "model-builder.txt",
    "constraint_builder": "constraint-builder.txt",
    "io": "io.txt",
    "integrator": "integrator.txt",
    "reviewer": "reviewer.txt",
    "fixer": "fixer.txt",
    "user_validator_explain": "user-validator-explain.txt",
    "user_validator_update": "user-validator-update.txt",
}


def load_prompt(agent: str) -> str:
    filename = _PROMPT_FILES.get(agent)
    if filename is None:
        raise ValueError(f"Unknown agent '{agent}'. Known agents: {list(_PROMPT_FILES)}")
    path = _PROMPT_DIR / filename
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        raise FileNotFoundError(f"prompt file for agent '{agent}' not found at {path}") from e
