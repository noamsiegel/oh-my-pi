"""Shared PR-review label normalization and matching policy."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


def normalize_label_name(raw: Any) -> str | None:
    """Return the exact normalized GitHub label name, or None when absent."""
    name: Any
    if isinstance(raw, str):
        name = raw
    elif isinstance(raw, Mapping):
        name = raw.get("name")
    else:
        return None
    if not isinstance(name, str):
        return None
    normalized = name.strip().lower()
    return normalized or None


def normalize_label_names(raw: Iterable[Any] | None) -> frozenset[str]:
    """Normalize a mixed iterable of GitHub label objects and plain strings."""
    if raw is None:
        return frozenset()
    names: set[str] = set()
    try:
        iterator = iter(raw)
    except TypeError:
        return frozenset()
    for item in iterator:
        name = normalize_label_name(item)
        if name is not None:
            names.add(name)
    return frozenset(names)


def payload_label_name(payload: Mapping[str, Any]) -> str | None:
    """Extract the normalized `label.name` from a GitHub webhook payload."""
    return normalize_label_name(payload.get("label"))


def matching_review_labels(labels: Iterable[str], allowlist: frozenset[str]) -> frozenset[str]:
    """Return exact normalized labels present in the configured review allowlist."""
    normalized = normalize_label_names(labels)
    return frozenset(label for label in normalized if label in allowlist)


def has_review_label(labels: Iterable[str], allowlist: frozenset[str]) -> bool:
    """Return whether any exact normalized label is review-enabled."""
    return bool(matching_review_labels(labels, allowlist))


__all__ = [
    "has_review_label",
    "matching_review_labels",
    "normalize_label_name",
    "normalize_label_names",
    "payload_label_name",
]
