"""Audit regression test — thinking-mode routing (finding #13).

Effort-capable 4.6+ models must route to adaptive thinking. Per the Anthropic API,
budget_tokens is deprecated on Sonnet 4.6 and removed (HTTP 400) on Opus 4.7/4.8
and Fable 5 — yet _uses_adaptive_thinking only matches 'opus-4', so Sonnet 4.6
(the default reviewer/fixer model) and Fable 5 take the budget_tokens path.
"""
import pytest

from vibesolve.agents.client import _uses_adaptive_thinking


@pytest.mark.parametrize("model", ["claude-sonnet-4-6", "claude-fable-5"])
def test_effort_capable_models_route_to_adaptive(model):
    assert _uses_adaptive_thinking(model) is True


@pytest.mark.parametrize("model", ["claude-opus-4-8", "claude-opus-4-6"])
def test_opus_already_routes_to_adaptive(model):
    # regression guard: these already work
    assert _uses_adaptive_thinking(model) is True
