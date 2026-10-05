"""Applicability boundary shared by research, compliance, and verification."""
from collections.abc import Mapping


def item_applies_to_module(item, module_id: str) -> bool:
    """Legacy discipline items are implicit; jurisdiction items fail closed.

    No keyword inference or category-wide broadcast: a shared fact controls
    only modules explicitly named by the researcher. Missing/malformed scope
    remains context, including after cache/pending-state deserialization.
    """
    def get(name, default=None):
        return item.get(name, default) if isinstance(item, Mapping) else getattr(item, name, default)

    scope = get("applicable_module_ids")
    if scope is None:
        return not str(get("dimension_id", "")).startswith("jurisdiction_")
    return (
        isinstance(scope, list)
        and all(isinstance(value, str) for value in scope)
        and bool(module_id)
        and module_id in scope
    )
