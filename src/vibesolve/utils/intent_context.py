"""Keep the user's original requirements alongside the model-derived spec."""

from dataclasses import dataclass, field


@dataclass
class IntentContext:
    original_request: str
    clarifications: list[str] = field(default_factory=list)

    def fields(self) -> dict[str, str | list[str]]:
        return {"OriginalRequest": self.original_request, "UserClarifications": self.clarifications}
