"""The model-agnostic boundary is a source-level fact, not a claim (TDR-003, ADR-0016).

If a vendor name or its HTTP client can be grepped out of the runtime, governance, ledger,
worker, API or SDK layers, then switching provider is not configuration only. This test keeps
vendor knowledge inside the adapter and the construction layer, where ADR-0016 rule 6 puts it.

It is the same style of guard as ``tests/architecture/test_T14_domain_independence.py``: a
negative self-test that must fail the moment the leak is introduced.
"""

from __future__ import annotations

import re
from pathlib import Path

CORE_ROOT = Path(__file__).resolve().parents[3] / "src" / "tether"

# The adapter owns OpenAI's wire format; the factory and settings name it as a configuration
# value. Nothing else in the core may mention it.
VENDOR_ALLOWED = frozenset(
    {
        Path("providers/openai_provider.py"),
        Path("providers/factory.py"),
        Path("settings.py"),
    }
)

# Only the adapter talks HTTP to a model provider. `observability/logging.py` silences the
# client's request logging by logger name, which is not an import.
HTTP_CLIENT_ALLOWED = frozenset({Path("providers/openai_provider.py")})
SDK_ALLOWED = frozenset({Path("providers/openai_provider.py")})

VENDOR = re.compile(r"openai", re.IGNORECASE)
#
# `httpx` and `httpx2` (the SDK's own transport) are both HTTP clients, and only the adapter may
# name either. Matching `httpx2?` followed by a word boundary stops `import httpx2` slipping past
# a pattern written for `httpx`.
HTTP_IMPORT = re.compile(r"^\s*(?:import|from)\s+httpx2?\b", re.MULTILINE)
# The vendor SDK itself. Only the adapter imports it, so no SDK type can reach another module.
SDK_IMPORT = re.compile(r"^\s*(?:import|from)\s+openai\b", re.MULTILINE)

# Layers that must stay vendor-blind for the product claim to be true.
GOVERNED_LAYERS = (
    "runtime",
    "policy",
    "registry",
    "ledger",
    "approvals",
    "reconciliation",
    "audit",
    "storage",
    "queue",
    "worker",
    "api",
    "sdk",
    "core",
    "identity",
    "authz",
)


def _core_sources() -> list[Path]:
    return sorted(path for path in CORE_ROOT.rglob("*.py") if "__pycache__" not in path.parts)


def test_no_vendor_name_outside_the_adapter_and_construction_layer() -> None:
    offenders = {
        path.relative_to(CORE_ROOT).as_posix()
        for path in _core_sources()
        if path.relative_to(CORE_ROOT) not in VENDOR_ALLOWED
        and VENDOR.search(path.read_text(encoding="utf-8"))
    }
    assert not offenders, f"OpenAI leaked outside the adapter: {sorted(offenders)}"


def test_the_governed_layers_never_mention_a_model_vendor() -> None:
    """The claim under test: governance does not know which model vendor is configured."""
    offenders = []
    for path in _core_sources():
        relative = path.relative_to(CORE_ROOT)
        if (
            relative.parts
            and relative.parts[0] in GOVERNED_LAYERS
            and VENDOR.search(path.read_text(encoding="utf-8"))
        ):
            offenders.append(relative.as_posix())
    assert not offenders, f"a model vendor reached the governed layers: {sorted(offenders)}"


def test_only_the_adapter_imports_the_http_client() -> None:
    offenders = {
        path.relative_to(CORE_ROOT).as_posix()
        for path in _core_sources()
        if path.relative_to(CORE_ROOT) not in HTTP_CLIENT_ALLOWED
        and HTTP_IMPORT.search(path.read_text(encoding="utf-8"))
    }
    assert not offenders, f"httpx imported outside the provider adapter: {sorted(offenders)}"


def test_the_runtime_depends_only_on_the_protocol() -> None:
    """The run driver and graph import the boundary, never a concrete hosted adapter."""
    for module in ("runtime/graph.py", "runtime/driver.py"):
        source = (CORE_ROOT / module).read_text(encoding="utf-8")
        assert "openai_provider" not in source, module
        assert "tether.providers.protocol" in source, module


def test_only_the_adapter_imports_the_vendor_sdk() -> None:
    """SDK types stay inside the adapter: the factory builds it but never imports the SDK."""
    offenders = {
        path.relative_to(CORE_ROOT).as_posix()
        for path in _core_sources()
        if path.relative_to(CORE_ROOT) not in SDK_ALLOWED
        and SDK_IMPORT.search(path.read_text(encoding="utf-8"))
    }
    assert not offenders, f"the vendor SDK was imported outside the adapter: {sorted(offenders)}"


def test_the_guard_patterns_catch_what_they_claim_to() -> None:
    """A checker that cannot fail proves nothing: pin the patterns on known positives."""
    assert HTTP_IMPORT.search("import httpx\n")
    assert HTTP_IMPORT.search("import httpx2\n")
    assert HTTP_IMPORT.search("from httpx2 import AsyncClient\n")
    assert HTTP_IMPORT.search("    import httpx2\n")
    assert not HTTP_IMPORT.search("import httpxy\n")
    assert SDK_IMPORT.search("import openai\n")
    assert SDK_IMPORT.search("from openai import AsyncOpenAI\n")
    assert SDK_IMPORT.search("    from openai.types import Response\n")
    assert not SDK_IMPORT.search("import openai_provider\n")
