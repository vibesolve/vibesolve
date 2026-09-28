from importlib import resources

_PROMPT_DIR = resources.files("vibesolve").joinpath("prompts")

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
    prompt = (_PROMPT_DIR / filename).read_text(encoding="utf-8")
    if agent != "parser":
        prompt += "\n\n" + (_PROMPT_DIR / "shared-intent.txt").read_text(encoding="utf-8")
    return prompt
