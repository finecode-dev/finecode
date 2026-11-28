"""Codex has no question channel: the opt-in key stays absent (C15, C20).

The shared `may_ask_questions` key is added per backend when that backend can
ask (#920-#922). Until #922 lands, the Codex config and profile must not grow
the field — and neither may the Pi or Claude ones here (those gain it in
#920/#921, so this file asserts only Codex).
"""

from __future__ import annotations

import dataclasses

import fine_agent

from fine_agent_codex.codex_agent_handler import (
    CodexAgentHandlerConfig,
    CodexAgentProfile,
)


def test_the_opt_in_key_is_absent_from_codex_config_and_profile() -> None:
    """Nothing offers what no backend implements yet."""
    for cls in (CodexAgentHandlerConfig, CodexAgentProfile):
        assert fine_agent.AGENT_QUESTION_OPT_IN_KEY not in {
            field.name for field in dataclasses.fields(cls)
        }
