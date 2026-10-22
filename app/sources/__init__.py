"""Adapter registry.

Adapters are registered by name and can be filtered by intent. A failing import
is recorded as an unavailable adapter instead of crashing the application, so a
partial environment still produces an honest search scope report.
"""

from __future__ import annotations

import threading
from typing import Callable, Iterable

from ..logging_utils import get_logger
from .base import (
    CLINICAL,
    DATASETS,
    GOVERNMENT,
    LITERATURE,
    REPOSITORY,
    AdapterError,
    NormalizedRecord,
    SearchPage,
    SearchQuery,
    SourceAdapter,
)

log = get_logger("sources.registry")

# name -> (module path, class name)
ADAPTER_SPECS: dict[str, tuple[str, str]] = {
    "openalex": ("openalex", "OpenAlexAdapter"),
    "crossref": ("crossref", "CrossrefAdapter"),
    "semantic_scholar": ("semantic_scholar", "SemanticScholarAdapter"),
    "europepmc": ("europepmc", "EuropePmcAdapter"),
    "huggingface": ("huggingface", "HuggingFaceAdapter"),
    "zenodo": ("zenodo", "ZenodoAdapter"),
    "datacite": ("datacite", "DataCiteAdapter"),
    "uci": ("uci", "UciAdapter"),
    "datagov": ("datagov", "DataGovAdapter"),
}

# Families queried for each intent, in priority order.
INTENT_FAMILIES: dict[str, tuple[str, ...]] = {
    "literature": (LITERATURE, CLINICAL),
    "datasets": (DATASETS, REPOSITORY, GOVERNMENT),
    "both": (LITERATURE, CLINICAL, DATASETS, REPOSITORY, GOVERNMENT),
}

RegistryBuilder = Callable[[str], SourceAdapter]


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, SourceAdapter] = {}
        self._unavailable: dict[str, str] = {}
        self._lock = threading.RLock()

    # -- population --------------------------------------------------------
    def register(self, adapter: SourceAdapter) -> None:
        with self._lock:
            self._adapters[adapter.name] = adapter

    def mark_unavailable(self, name: str, reason: str) -> None:
        with self._lock:
            self._unavailable[name] = reason

    # -- lookup ------------------------------------------------------------
    def get(self, name: str) -> SourceAdapter | None:
        return self._adapters.get(name)

    def all(self) -> list[SourceAdapter]:
        return [self._adapters[key] for key in sorted(self._adapters)]

    def select(
        self,
        *,
        names: Iterable[str] | None = None,
        intent: str = "both",
        families: Iterable[str] | None = None,
    ) -> list[SourceAdapter]:
        selected: list[SourceAdapter] = []
        requested = {n.strip() for n in names if n.strip()} if names else None
        allowed_families = set(families) if families else set(INTENT_FAMILIES.get(intent, ()))

        for adapter in self.all():
            if requested is not None and adapter.name not in requested:
                continue
            if requested is None:
                if not allowed_families.intersection(adapter.families):
                    continue
                if not adapter.supports_intent(intent):
                    continue
            selected.append(adapter)
        return selected

    def unavailable(self) -> dict[str, str]:
        return dict(self._unavailable)

    def describe(self) -> list[dict]:
        described = []
        for adapter in self.all():
            info = adapter.describe()
            info["available"] = True
            described.append(info)
        for name, reason in sorted(self._unavailable.items()):
            described.append(
                {
                    "name": name,
                    "label": name,
                    "available": False,
                    "credentials_present": False,
                    "notes": reason,
                    "families": [],
                }
            )
        return described


_REGISTRY: AdapterRegistry | None = None
_REGISTRY_LOCK = threading.Lock()


def build_registry() -> AdapterRegistry:
    registry = AdapterRegistry()
    for name, (module_name, class_name) in ADAPTER_SPECS.items():
        try:
            module = __import__(f"{__name__}.{module_name}", fromlist=[class_name])
            adapter_class = getattr(module, class_name)
            registry.register(adapter_class())
        except Exception as exc:  # pragma: no cover - defensive
            log.error("adapter_unavailable", adapter=name, error=str(exc))
            registry.mark_unavailable(name, f"adapter failed to initialise: {exc}")
    return registry


def get_registry() -> AdapterRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        with _REGISTRY_LOCK:
            if _REGISTRY is None:
                _REGISTRY = build_registry()
    return _REGISTRY


def reset_registry() -> None:
    global _REGISTRY
    with _REGISTRY_LOCK:
        _REGISTRY = None


__all__ = [
    "AdapterError",
    "AdapterRegistry",
    "NormalizedRecord",
    "SearchPage",
    "SearchQuery",
    "SourceAdapter",
    "build_registry",
    "get_registry",
    "reset_registry",
]
