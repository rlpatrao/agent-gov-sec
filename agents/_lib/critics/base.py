"""Shared utilities for Discovery critics."""
from __future__ import annotations

import json

from core.discovery_artifacts import CriticReport


def parse_critic_response(text: str) -> CriticReport:
    text = text.strip()
    if text.startswith("```"):
        nl = text.find("\n")
        text = text[nl + 1:] if nl != -1 else text
        if text.endswith("```"):
            text = text[:-3]
    data = json.loads(text)
    return CriticReport.model_validate(data)
