"""Dev-only issuer keys (ADR-0013 rule 5). Never part of the Tether image (see .dockerignore).

Creates one Ed25519 signing key per audience in ``tools/dev_issuer/.keys/`` (git-ignored) if it is
missing, and prints the bootstrap ``issuers`` block holding only the public halves.

    uv run python tools/dev_issuer/keys.py ops-demo
"""

from __future__ import annotations

import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

KEYS = Path(__file__).parent / ".keys"
AUDIENCES = ("tether-tasks", "tether-approvals", "tether-ops")


def _out(line: str) -> None:
    sys.stdout.write(f"{line}\n")


def issuer_url(workspace: str, audience: str) -> str:
    return f"https://dev-issuer.tether.local/{workspace}/{audience}"


def key_path(workspace: str, audience: str) -> Path:
    return KEYS / f"{workspace}.{audience}.pem"


def load_or_create(workspace: str, audience: str) -> ed25519.Ed25519PrivateKey:
    path = key_path(workspace, audience)
    if path.exists():
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(key, ed25519.Ed25519PrivateKey):
            raise SystemExit(f"{path} is not an Ed25519 key")
        return key
    KEYS.mkdir(exist_ok=True)
    key = ed25519.Ed25519PrivateKey.generate()
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return key


def main(workspace: str) -> None:
    _out("    issuers:")
    for audience in AUDIENCES:
        public = (
            load_or_create(workspace, audience)
            .public_key()
            .public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            .decode()
        )
        short = audience.removeprefix("tether-")
        _out(f"      - id: {short}")
        _out(f"        iss: {issuer_url(workspace, audience)}")
        _out(f"        audiences: [{audience}]")
        _out("        algorithm: EdDSA")
        _out("        public_keys:")
        _out(f"          {short}-1: |")
        for line in public.strip().splitlines():
            _out(f"            {line}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "ops-demo")
