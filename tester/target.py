"""The only path from the tester to a target: allowlisted, bounded, no redirects, no secrets out.

DNS is checked before each request, but HTTPX resolves again when connecting, so a target
that rebinds between the two lookups is not prevented here. Production deployments should
also restrict tester egress at the network layer.
"""

import ipaddress
import socket
import time
from collections.abc import Callable, Mapping
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

from app.config import AppEnvironment
from tester.config import TargetAuth, validate_base_url

Identity = Literal["userA", "userB", "reviewer", "test-admin"]
Resolver = Callable[..., list[Any]]

GET_RETRIES = 3
FORBIDDEN_ADDRESSES = {
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("fd00:ec2::254"),
}


class CommunicationError(Exception):
    """No usable response arrived (timeout, connection failure, refused address)."""


class DeadlineExceeded(Exception):
    """The overall tester budget is spent; the tester, not the target, stops work."""


def check_address(host: str, environment: AppEnvironment, resolver: Resolver) -> None:
    try:
        infos = resolver(host, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise CommunicationError("target host could not be resolved") from exc
    if not infos:
        raise CommunicationError("target host could not be resolved")
    for info in infos:
        address = ipaddress.ip_address(str(info[4][0]).split("%")[0])
        if (
            address in FORBIDDEN_ADDRESSES
            or address.is_link_local
            or address.is_unspecified
            or address.is_multicast
            or address.is_reserved
            or (
                environment == AppEnvironment.PRODUCTION
                and (address.is_loopback or address.is_private)
            )
        ):
            raise CommunicationError("target resolved to a forbidden address")


class TargetClient:
    def __init__(
        self,
        base_url: str,
        access: TargetAuth,
        environment: AppEnvironment,
        deadline: float,
        *,
        transport: httpx.BaseTransport | None = None,
        resolver: Resolver = socket.getaddrinfo,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        validate_base_url(base_url, environment)
        self.base_url = base_url.rstrip("/")
        self.host = urlsplit(base_url).hostname or ""
        self.environment = environment
        self.deadline = deadline
        self.session_id: str | None = None
        self._access = access
        self._resolver = resolver
        self._clock = clock
        self._client = httpx.Client(
            transport=transport, trust_env=False, verify=True, follow_redirects=False
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "TargetClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def remaining(self) -> float:
        return self.deadline - self._clock()

    def _token(self, identity: Identity) -> str:
        secret = {
            "userA": self._access.user_a,
            "userB": self._access.user_b,
            "reviewer": self._access.reviewer,
            "test-admin": self._access.test_admin,
        }[identity]
        return secret.get_secret_value()

    def request(
        self,
        method: Literal["GET", "POST"],
        path: str,
        *,
        identity: Identity | None = None,
        raw_token: str | None = None,
        session_header: bool = True,
        headers: Mapping[str, str] | None = None,
        timeout: float = 10,
        **kwargs: Any,
    ) -> httpx.Response:
        """One bounded call. Only GET is retried, and only after transport failures."""
        request_headers: dict[str, str] = {}
        token = raw_token if raw_token is not None else (identity and self._token(identity))
        if token:
            request_headers["Authorization"] = f"Bearer {token}"
        if session_header and self.session_id and not path.startswith("/test/"):
            request_headers["X-Test-Session"] = self.session_id
        request_headers.update(headers or {})
        attempts = GET_RETRIES + 1 if method == "GET" else 1
        for attempt in range(attempts):
            budget = min(timeout, self.remaining())
            if budget <= 0:
                raise DeadlineExceeded("tester deadline reached before the request")
            check_address(self.host, self.environment, self._resolver)
            try:
                return self._client.request(
                    method,
                    self.base_url + path,
                    headers=request_headers,
                    timeout=budget,
                    **kwargs,
                )
            except httpx.TransportError as exc:
                if attempt + 1 == attempts:
                    # Never include the request (and so its headers) in the message.
                    raise CommunicationError(f"{method} {path}: {type(exc).__name__}") from None
        raise AssertionError("unreachable")
