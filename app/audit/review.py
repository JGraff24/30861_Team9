"""Phase 4 rule output: findings paired with their review context and correction plans."""

from dataclasses import dataclass, field
from typing import Literal

from pydantic import JsonValue

from app.contracts.models import Finding, FindingClassification, ReviewItem
from app.workbooks.refs import Rect

XmlPath = tuple[tuple[str, int], ...]
CorrectionKind = Literal["alt", "rename", "link", "header_new", "header_enable"]
OWNER_ACTIONS: list[Literal["APPLY", "DECLINE", "MANUAL_REVIEW"]] = [
    "APPLY",
    "DECLINE",
    "MANUAL_REVIEW",
]
MANUAL_ACTIONS: list[Literal["APPLY", "DECLINE", "MANUAL_REVIEW"]] = ["DECLINE", "MANUAL_REVIEW"]


@dataclass(frozen=True)
class Correction:
    """Where and how a supported correction edits the source package."""

    kind: CorrectionKind
    part: str
    object_id: str | None = None  # alt: the unique cNvPr id
    sheet_name: str | None = None  # rename: the source sheet name
    sheet_position: int | None = None  # rename: <sheet> element index
    cell: tuple[int, int] | None = None  # link: the labelled cell
    cell_path: XmlPath | None = None
    link_path: XmlPath | None = None
    rect: Rect | None = None  # header: candidate/table rectangle
    labels: tuple[str, ...] = ()  # header_new: first-row labels (table column names)


@dataclass
class Entry:
    finding: Finding
    current: dict[str, JsonValue] | None = None
    reason: str = "Review this item manually; no supported automatic correction applies."
    correction: Correction | None = None


@dataclass
class RuleOutput:
    entries: list[Entry] = field(default_factory=list)
    limitations: set[str] = field(default_factory=set)

    def extend(self, other: "RuleOutput") -> None:
        self.entries.extend(other.entries)
        self.limitations.update(other.limitations)


def review_item(entry: Entry) -> ReviewItem:
    editable = entry.finding.classification == FindingClassification.REQUIRES_USER_INPUT
    return ReviewItem(
        findingId=entry.finding.findingId,
        allowedActions=list(OWNER_ACTIONS if editable else MANUAL_ACTIONS),
        currentValue=entry.current,
        reason=entry.reason,
    )


def display(text: str, limit: int = 80) -> str:
    """Single-line, bounded rendering of workbook text inside finding prose."""
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"
