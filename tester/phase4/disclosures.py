"""Independent minimum factual predicates for the specification's required disclosures.

These predicates do not require target prose to equal the fixture author's sentences. Each
group states a necessary concept, with alternative vocabulary; all groups must be present.
They reject absent or irrelevant prose and leave accuracy/clarity to the recorded human review.
No production engine messages or helper output supplies this oracle.
"""

import re

SCOPE = (
    r"single.?cell|one.?cell|one.?row|one.?column|single.?row|single.?column",
    r"table|infer",
    r"not|exclud|scope|only|outside",
)
DISPLAY = (r"missing|unspecified|default|assum|absent|without", r"white", r"black")
LOGICAL = (r"table|header|structure", r"cannot|could not|not|unsafe|ambig|manual|unsupported")
DUPLICATES = (
    r"duplicat|repeat|non.?unique",
    r"\bid\b|\bids\b|identifier",
    r"xml|index|target|path",
)
FORMULA_TEXT = (r"formula", r"not|cannot|without|cached", r"calculat|evaluat|render|cached")
FORMULA_LINK = (r"hyperlink|formula.?link", r"formula", r"not|cannot|manual|unsupported")
INDIRECT = (r"indirect", r"rename|sheet", r"manual|unsupported|safe|not|cannot")
COMPUTED_LINK = (
    r"hyperlink",
    r"computed|dynamic|localiz|unsupported|formula",
    r"not|cannot|manual",
)
UNSUPPORTED_COLOR = (
    r"fill|gradient|pattern",
    r"rich.?text|mixed",
    r"automatic|auto.?colo",
    r"ratio|contrast",
    r"manual|cannot|not|unsupported|unknown",
)
CONDITIONAL = (r"conditional", r"evaluat|render|contrast|manual", r"not|manual|cannot")


def requirements(fact: str) -> tuple[str, ...]:
    if fact.startswith("Single-cell,"):
        return SCOPE
    if fact.startswith("Missing fill"):
        return DISPLAY
    if fact.startswith("Logical table structure"):
        return LOGICAL
    if fact.startswith("Repeated drawing object IDs"):
        return DUPLICATES
    if fact == "Formula text was not calculated.":
        return FORMULA_TEXT
    if fact == "Formula-based hyperlinks are not modified.":
        return FORMULA_LINK
    if fact.startswith("INDIRECT reference"):
        return INDIRECT
    if fact.startswith("Computed or localized HYPERLINK"):
        return COMPUTED_LINK
    if fact.startswith("Unsupported fills, mixed rich text"):
        return UNSUPPORTED_COLOR
    if fact.startswith("Visual conditional formatting"):
        return CONDITIONAL
    raise ValueError("fixture declares a limitation fact without an independent predicate")


def missing_facts(facts: list[str], limitations: list[str]) -> list[str]:
    text = "\n".join(limitations).casefold()
    return [
        fact
        for fact in facts
        if any(not re.search(pattern, text) for pattern in requirements(fact))
    ]
