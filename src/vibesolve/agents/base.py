"""The caller interface the pipeline depends on."""

from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

import structlog
from pydantic import BaseModel

from vibesolve.config.settings import AgentModelConfig


T = TypeVar("T", bound=BaseModel)


class BaseAgentCaller(ABC):
    agent_times: dict[str, float]
    # Per-role totals across every attempt, including failures. Input includes
    # cache hits; cached_input_tokens is a subset, never an additional charge.
    agent_tokens: dict[str, dict]

    def close(self) -> None:
        """Release resources owned by this problem's caller."""

    def last_model_config_for(self, agent: str) -> AgentModelConfig | None:
        """Return the model config used by the agent's last call, if any."""
        return None

    @abstractmethod
    def call_typed(self, agent: str, user_message: str, model_type: type[T]) -> T:
        """Return a validated role result; the implementation owns bounded retries."""


AgentCallerFactory = Callable[[Path, structlog.BoundLogger], BaseAgentCaller]
