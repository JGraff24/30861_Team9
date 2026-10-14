"""The tester's own findingId oracle, written from the contract, never imported from the engine."""

import hashlib
import json


def finding_id(
    source_sha256: str, ruleset_version: str, rule_id: str, worksheet: str, target: str
) -> str:
    payload = [source_sha256, ruleset_version, rule_id, worksheet, target]
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
