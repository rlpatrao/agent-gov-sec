"""TransformerFactory — port of `src/transformer/`.

Maps raw upstream event payload to jurisdiction+service-type specific filer JSON.
Looked up by `{jurisdiction}_{serviceType}`.
"""
from __future__ import annotations

from typing import Callable

from .helper import remove_trailing_spaces

_Transformer = Callable[[dict], dict]

_REGISTRY: dict[str, _Transformer] = {}


def register(key: str) -> Callable[[_Transformer], _Transformer]:
    def _decorator(fn: _Transformer) -> _Transformer:
        _REGISTRY[key] = fn
        return fn
    return _decorator


def _default_transformer(raw: dict) -> dict:
    return {
        "entity": raw.get("entity", {}),
        "service": raw.get("service", {}),
        "order": raw.get("order", {}),
        "filingMetadata": raw.get("filingMetadata", {}),
    }


class TransformerFactory:
    @staticmethod
    def transform_to_filer_json(raw_payload: dict, service_type: str,
                                jurisdiction: str, source_system: str = "UPSTREAM") -> dict:
        payload = remove_trailing_spaces(raw_payload)
        key = f"{jurisdiction}_{service_type}"
        fn = _REGISTRY.get(key, _default_transformer)
        merged = fn(payload)
        merged["_meta"] = {
            "serviceType": service_type,
            "jurisdiction": jurisdiction,
            "sourceSystem": source_system,
        }
        return merged
