"""Grader/operator authentication by SHA256 token hash; raw tokens are never stored."""

import hashlib
import hmac

from app.contracts.models import ErrorSource
from tester.errors import TesterError


def operator_subject(token: str | None, token_hashes: dict[str, str]) -> str:
    if token is None or not token.strip():
        raise TesterError(401, "Missing or invalid operator credential.", ErrorSource.TESTER)
    digest = hashlib.sha256(token.strip().encode("utf-8")).hexdigest()
    subject = None
    # Compare against every configured hash so timing does not reveal which digest matched.
    for known, candidate in token_hashes.items():
        if hmac.compare_digest(known, digest):
            subject = candidate
    if subject is None:
        raise TesterError(401, "Missing or invalid operator credential.", ErrorSource.TESTER)
    return subject
