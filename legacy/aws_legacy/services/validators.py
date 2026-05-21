"""Validator layer — port of `src/validator/validators/validators.exporters.ts`.

Each validator: (merged_json) -> {"success": bool, "errors": [str]}.
Lookup key priority: {juris}_{entity}_{service} -> {juris}_{service} -> {service}.
"""
from __future__ import annotations

from typing import Callable

_Validator = Callable[[dict], dict]

_VALIDATORS: dict[str, _Validator] = {}


def register(key: str) -> Callable[[_Validator], _Validator]:
    def _decorator(fn: _Validator) -> _Validator:
        _VALIDATORS[key] = fn
        return fn
    return _decorator


def _default_validator(merged: dict) -> dict:
    errors: list[str] = []
    entity = merged.get("entity") or {}
    if not entity.get("name"):
        errors.append("entity.name is required")
    if not merged.get("_meta", {}).get("jurisdiction"):
        errors.append("jurisdiction is required")
    return {"success": not errors, "errors": errors}


def validate(merged_json: dict) -> dict:
    meta = merged_json.get("_meta", {})
    jurisdiction = meta.get("jurisdiction", "")
    service_type = meta.get("serviceType", "")
    entity_type = (merged_json.get("entity") or {}).get("type", "")

    for key in (
        f"{jurisdiction}_{entity_type}_{service_type}",
        f"{jurisdiction}_{service_type}",
        f"{service_type}",
    ):
        if key and key in _VALIDATORS:
            return _VALIDATORS[key](merged_json)
    return _default_validator(merged_json)
