"""Dev-only token minting (ADR-0013 rule 5). Never part of the Tether image.

Signs a short-lived JWT with a key created by ``keys.py``:

    uv run python tools/dev_issuer/mint.py alice --aud tether-tasks --role operator
    uv run python tools/dev_issuer/mint.py bob --aud tether-approvals --role approver
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid

import jwt
from keys import AUDIENCES, issuer_url, load_or_create


def _out(line: str) -> None:
    sys.stdout.write(f"{line}\n")


def mint(workspace: str, audience: str, sub: str, roles: list[str], ttl: int) -> str:
    now = int(time.time())
    claims = {
        "iss": issuer_url(workspace, audience),
        "aud": audience,
        "sub": sub,
        "workspace": workspace,
        "roles": roles,
        "iat": now,
        "exp": now + ttl,
        "jti": str(uuid.uuid4()),
    }
    key = load_or_create(workspace, audience)
    kid = f"{audience.removeprefix('tether-')}-1"
    return jwt.encode(claims, key, algorithm="EdDSA", headers={"kid": kid})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sub")
    parser.add_argument("--aud", required=True, choices=AUDIENCES)
    parser.add_argument("--role", action="append", default=[])
    parser.add_argument("--workspace", default="ops-demo")
    parser.add_argument("--ttl", type=int, default=900)
    args = parser.parse_args()
    _out(mint(args.workspace, args.aud, args.sub, args.role, args.ttl))
