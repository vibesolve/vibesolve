"""Messages exchanged with the Pi worker."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from vibesolve.config.settings import EffortLevel


MAX_MESSAGE_BYTES = 16 * 1024 * 1024


class PiMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PiStageRequest(PiMessage):
    id: int = Field(ge=1)
    provider: str = Field(min_length=1, max_length=120)
    model: str = Field(min_length=1, max_length=300)
    effort: EffortLevel
    system: str
    user: str
    result_schema: dict[str, JsonValue]
    seconds: int = Field(default=600, ge=1, le=600)


class PiUsage(PiMessage):
    provider: str
    model: str
    input_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(ge=0)
    cache_write_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    stop_reason: str

    @model_validator(mode="after")
    def _cache_is_part_of_input(self) -> "PiUsage":
        if self.cached_input_tokens + self.cache_write_tokens > self.input_tokens:
            raise ValueError("Cache counters must be included in total input tokens")
        return self


class PiUsageEvent(PiMessage):
    type: Literal["usage"]
    id: int = Field(ge=1)
    usage: PiUsage


class PiStageResult(PiMessage):
    type: Literal["result"]
    id: int = Field(ge=1)
    ok: bool
    text: str | None = None
    error: str | None = None

    @model_validator(mode="after")
    def _coherent_outcome(self) -> "PiStageResult":
        if self.ok:
            if self.text is None or self.error is not None:
                raise ValueError("Successful completion must supply text without an error")
        elif not self.error or self.text is not None:
            raise ValueError("Failed stage must supply an error without accepted output")
        return self


PiEvent = Annotated[PiUsageEvent | PiStageResult, Field(discriminator="type")]
