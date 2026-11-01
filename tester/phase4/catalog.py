"""Stable individual IDs for grouped assertion-inventory vectors.

The first variant retains the historical inventory ID; every additional variant has an
explicit suffix. Handlers and manifest generation use this independent catalog together.
"""

DECISION_VARIANTS = {
    "unknown_field": ("envelope", "decision"),
    "value_wrong_shape": ("string", "other_rule", "unknown_field", "null"),
    "value_on_decline": ("decline", "manual_review"),
    "alt_blank": ("title", "description"),
    "alt_too_long": ("title", "description"),
    "name_invalid": (
        "too_long",
        "edge_whitespace",
        "separator",
        "generic",
        "reserved",
        "case_only",
        "unchanged",
        "source_collision",
    ),
    "header_row": ("wrong_row", "boolean", "replacement_labels"),
    "manual_apply": (
        "decorative_alt",
        "duplicate_alt",
        "color",
        "formula_link",
        "merge",
        "unsafe_rename",
    ),
    "missing_key": ("missing", "malformed"),
}
FAULT_AUTH = ("user", "reviewer", "unauthenticated", "invalid_token")
FAULT_INVALID = ("empty", "two", "zero", "boolean", "string", "extra_field", "unknown_field")
REAUDIT_FORMS = ("absent", "empty_object", "reason")
REAUDIT_INVALID = ("wrong_reason", "unknown_field")
TINT_CASES = ("standard", "blue_negative", "blue_positive", "gray_one", "gray_five")


def variants(test_id: str) -> tuple[str, ...]:
    if test_id == "P4-FAULT-AUTH":
        return FAULT_AUTH
    if test_id == "P4-FAULT-INVALID":
        return FAULT_INVALID
    if test_id == "P4-REAUDIT":
        return REAUDIT_FORMS
    if test_id == "P4-REAUDIT-INVALID":
        return REAUDIT_INVALID
    if test_id == "P4-RULE-COLOR-THEME-TINT":
        return TINT_CASES
    if test_id.startswith("P4-REJECT-"):
        return DECISION_VARIANTS.get(
            test_id.removeprefix("P4-REJECT-").lower().replace("-", "_"), ()
        )
    return ()


def select_variant[T](entries: list[T], names: tuple[str, ...], selected: object) -> list[T]:
    if len(entries) != len(names):
        raise ValueError("independent variant catalog differs from the implemented requests")
    if selected is None:
        return entries
    if selected not in names:
        raise ValueError("unknown planned vector variant")
    return [entries[names.index(str(selected))]]
