from __future__ import annotations

from robomp.pr_review_policy import (
    has_review_label,
    matching_review_labels,
    normalize_label_name,
    normalize_label_names,
    payload_label_name,
)


def test_normalize_label_name_accepts_objects_and_strings() -> None:
    assert normalize_label_name({"name": " Robo-Review "}) == "robo-review"
    assert normalize_label_name(" mail ") == "mail"


def test_normalize_label_name_ignores_blank_and_invalid_values() -> None:
    assert normalize_label_name({"name": "   "}) is None
    assert normalize_label_name({"not_name": "robo-review"}) is None
    assert normalize_label_name(42) is None


def test_normalize_label_names_accepts_mixed_iterables() -> None:
    assert normalize_label_names([{"name": "Robo-Review"}, " mail ", {"name": ""}, object(), {"name": None}]) == frozenset(
        {"robo-review", "mail"}
    )
    assert normalize_label_names(None) == frozenset()


def test_payload_label_name_reads_github_label_object() -> None:
    assert payload_label_name({"label": {"name": " Review:Robo-Review "}}) == "review:robo-review"
    assert payload_label_name({"label": "robo-review"}) == "robo-review"
    assert payload_label_name({}) is None


def test_review_label_matching_is_case_insensitive_and_exact() -> None:
    allowlist = frozenset({"robo-review", "hoa", "mail"})
    labels = [" Robo-Review ", "HOA", " mail ", "mailroom", "review:robo-review", "robo-review-extra"]
    assert matching_review_labels(labels, allowlist) == frozenset({"robo-review", "hoa", "mail"})
    assert has_review_label(labels, allowlist)


def test_review_label_matching_rejects_prefixes() -> None:
    allowlist = frozenset({"robo-review", "hoa", "mail"})
    assert matching_review_labels(["robo-review-extra", "mailroom", "review:robo-review"], allowlist) == frozenset()
    assert not has_review_label(["robo-review-extra", "mailroom", "review:robo-review"], allowlist)
