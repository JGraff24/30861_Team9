"""Opt-in private local credentials for future implementations; never auto-run."""

import argparse
import hashlib
import json
import os
import secrets
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-only", action="store_true", required=True)
    parser.add_argument("--output", type=Path, default=Path(".env.local"))
    args = parser.parse_args()
    if os.environ.get("APP_ENV", "development") not in {"development", "test"}:
        parser.error("development token generation requires APP_ENV=development or test")
    if args.output.name != ".env.local":
        parser.error("output must be a private ignored .env.local file")
    tokens = {
        name: secrets.token_urlsafe(32)
        for name in ("userA", "userB", "reviewer", "test-admin", "operator")
    }

    def token_hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    principals = {
        token_hash(tokens[name]): {"subject": f"local-{name}", "role": role}
        for name, role in (
            ("userA", "user"),
            ("userB", "user"),
            ("reviewer", "reviewer"),
            ("test-admin", "test_admin"),
        )
    }
    values = {
        "APP_ENV": "development",
        "SYSTEM_TOKEN_HASHES_JSON": json.dumps(principals),
        "REVIEWER_GRANTS_JSON": json.dumps({"local-reviewer": ["local-userA"]}),
        "TEST_SESSION_USERS_JSON": json.dumps(
            {"local-test-admin": ["local-userA", "local-userB", "local-reviewer"]}
        ),
        "TESTER_OPERATOR_TOKEN_HASHES_JSON": json.dumps(
            {token_hash(tokens["operator"]): "local-operator"}
        ),
        "ALLOWED_TARGET_URLS": json.dumps(["http://127.0.0.1:8000"]),
        "TARGET_AUTH_MAP_JSON": json.dumps(
            {
                "http://127.0.0.1:8000": {
                    key: tokens[key] for key in ("userA", "userB", "reviewer", "test-admin")
                }
            }
        ),
        "LOCAL_OPERATOR_TOKEN": tokens["operator"],
    }
    # Exclusive create prevents overwriting the user's environment; POSIX mode is private.
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        output.write("# PRIVATE local credentials. Do not commit or use on a deployed host.\n")
        for key, value in values.items():
            output.write(f"{key}='{value}'\n")
    print("Private local environment written; feature authentication remains pending.")


if __name__ == "__main__":
    main()
