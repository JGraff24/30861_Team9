"""Configuration-only bearer identities and explicit reviewer grants."""

import hashlib
import hmac

from app.config import SystemSettings
from app.contracts.errors import ProjectException
from app.contracts.models import Principal, Role


class TokenAuthService:
    def __init__(self, settings: SystemSettings) -> None:
        self._tokens = tuple(settings.system_token_hashes.items())
        self._grants = settings.reviewer_grants

    def authenticate(self, token: str | None) -> Principal:
        digest = hashlib.sha256((token or "").encode()).hexdigest()
        found = None
        for expected, principal in self._tokens:
            if hmac.compare_digest(expected, digest):
                found = principal
        if found is None or not token:
            raise ProjectException(401, "UNAUTHORIZED", "A valid bearer credential is required.")
        return found

    def require_user(self, token: str | None) -> Principal:
        principal = self.authenticate(token)
        if principal.role == Role.TEST_ADMIN:
            raise ProjectException(403, "FORBIDDEN", "This credential has testing powers only.")
        return principal

    def require_test_admin(self, token: str | None) -> Principal:
        principal = self.authenticate(token)
        if principal.role != Role.TEST_ADMIN:
            raise ProjectException(403, "FORBIDDEN", "A test administrator is required.")
        return principal

    def can_read(self, owner_subject: str, principal: Principal) -> bool:
        return principal.role != Role.TEST_ADMIN and (
            principal.subject == owner_subject
            or (
                principal.role == Role.REVIEWER
                and owner_subject in self._grants.get(principal.subject, [])
            )
        )
