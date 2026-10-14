"""Independent standard-library golden vectors, not Dylan's production engine."""

import hashlib
import json

VECTORS = (
    ("Expenses", "502d6624551c9da5c9a80d7efcc04be1119dad4a16f55171487949600a177714"),
    ("Dépenses 📊", "65c48a7e1beb35c5e0068830b884d5d6f658793be5bcf21a1189a134f592ddc3"),
    ("De\u0301penses 📊", "780ab5b340613108fa0dd216616b987cf1929a01b527495cbf3d9e63057fd9e7"),
)


def check_vectors() -> list[str]:
    digests = []
    for worksheet, expected in VECTORS:
        value = ["0" * 64, "1.0.0", "ALT_TEXT", worksheet, "xl/drawings/drawing1.xml#id=7"]
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        actual = hashlib.sha256(encoded).hexdigest()
        if actual != expected:
            raise ValueError(f"golden vector mismatch for {worksheet!r}")
        digests.append(actual)
    return digests


if __name__ == "__main__":
    for digest in check_vectors():
        print(digest)
