"""Own-team versus cross-team report profiles and design-model coverage (specification 3.0.0 §14).

The profile comes only from trusted tester configuration: the tester's own team ID and an exact
approved-target-URL to team-ID map. It is never inferred from host similarity, never taken from
a caller field, and is frozen with the session plan at setup. An own-team run requires the
team's complete, valid design-requirements map; the report then carries `coverage.model`. A
cross-team run omits the key entirely and needs no access to the other team's design map.
"""

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from app.contracts.models import CoverageMeasure
from tester.phase4 import contract

ProfileKind = Literal["own", "cross"]


class ProfileError(Exception):
    """Trusted profile configuration is missing or invalid; setup must fail (500/tester)."""


@dataclass(frozen=True)
class RunProfile:
    kind: ProfileKind
    tester_team: str
    target_team: str
    design_map: dict[str, Any] | None = None
    design_map_sha256: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "tester_team": self.tester_team,
            "target_team": self.target_team,
            "design_map": self.design_map,
            "design_map_sha256": self.design_map_sha256,
        }

    @classmethod
    def restore(cls, saved: dict[str, Any]) -> "RunProfile":
        kind = saved.get("kind")
        if kind not in ("own", "cross"):
            raise ProfileError("stored profile kind is invalid")
        profile = cls(
            kind=kind,
            tester_team=str(saved["tester_team"]),
            target_team=str(saved["target_team"]),
            design_map=saved.get("design_map"),
            design_map_sha256=saved.get("design_map_sha256"),
        )
        if (profile.kind == "own") != (profile.design_map is not None):
            raise ProfileError("stored own-team profile lacks its frozen design map")
        return profile

    @property
    def requirements(self) -> list[dict[str, Any]]:
        return list(self.design_map["requirements"]) if self.design_map else []


def load_design_map(
    path: Path | None, tester_team: str, planned_ids: Iterable[str]
) -> tuple[dict[str, Any], str]:
    """Read, validate and fingerprint the team's own design map; any defect is fatal."""
    if path is None:
        raise ProfileError("own-team run has no configured design map")
    try:
        raw = path.read_bytes()
    except OSError:
        raise ProfileError("own-team design map cannot be read") from None
    with contract.use_pack("phase5"):
        try:
            document = contract.parse_json(raw, "TeamDesignMap")
        except contract.ContractViolation as exc:
            raise ProfileError(f"design map is invalid: {exc}") from None
    if document["teamId"] != tester_team:
        raise ProfileError("design map belongs to a different team")
    ids = [requirement["requirementId"] for requirement in document["requirements"]]
    if len(ids) != len(set(ids)):
        raise ProfileError("design requirement IDs must be unique")
    planned = set(planned_ids)
    for requirement in document["requirements"]:
        unknown = set(requirement["testIds"]) - planned
        if unknown:
            raise ProfileError(
                f"design requirement {requirement['requirementId']} maps to unplanned test IDs"
            )
    return document, hashlib.sha256(raw).hexdigest()


def classify(
    target_url: str,
    *,
    tester_team: str | None,
    target_teams: dict[str, str],
    design_map_path: Path | None,
    planned_ids: Iterable[str],
) -> RunProfile:
    """Exact trusted classification; the design map is required only for own-team runs."""
    if not tester_team:
        raise ProfileError("tester team identity is not configured")
    target_team = target_teams.get(target_url)
    if not target_team:
        raise ProfileError("approved target has no configured team identity")
    if target_team != tester_team:
        return RunProfile("cross", tester_team, target_team)
    document, digest = load_design_map(design_map_path, tester_team, planned_ids)
    return RunProfile("own", tester_team, target_team, document, digest)


def model_coverage(profile: RunProfile, executed: set[str]) -> CoverageMeasure:
    """Each design requirement counts once when a mapped test executed a qualifying assertion.

    `executed` holds only tests that recorded a qualifying assertion and were not skipped; the
    denominator is every listed requirement, including those with no mapped tests yet.
    """
    if profile.design_map is None:
        raise ProfileError("model coverage applies only to own-team runs")
    requirements = profile.requirements
    covered = sum(1 for item in requirements if set(item["testIds"]) & executed)
    design = profile.design_map
    return CoverageMeasure(
        covered=covered,
        total=len(requirements),
        basis=(
            f"Team {design['teamId']} Phase 5 design version {design['designVersion']}: all "
            f"{len(requirements)} design requirements (map SHA256 "
            f"{profile.design_map_sha256}); requirements-to-tests mapping {design['mappingUrl']}. "
            "A requirement counts once when at least one mapped test executed its qualifying "
            "assertion in this session, even if it failed; setup, skips and unmapped or "
            "untested requirements earn nothing."
        ),
    )


def check_report_profile(body: object, kind: ProfileKind) -> None:
    """Raise ValueError unless a serialized report matches its frozen own/cross profile."""
    definition = "OwnTeamSuiteReport" if kind == "own" else "CrossTeamSuiteReport"
    with contract.use_pack("phase5"):
        problems = contract.violations(definition, body)
    if problems:
        raise ValueError(f"report does not match {definition}: {problems[0]}")
    coverage = body["coverage"] if isinstance(body, dict) else {}
    for name, measure in coverage.items():
        covered, total = measure["covered"], measure["total"]
        if (covered is None) != (total is None) or (total is not None and covered > total):
            raise ValueError(f"coverage.{name} counts are inconsistent")
    if kind == "cross" and "model" in coverage:
        raise ValueError("cross-team report contains coverage.model")
    if kind == "own" and "model" not in coverage:
        raise ValueError("own-team report lacks coverage.model")


def canonical(document: object) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
