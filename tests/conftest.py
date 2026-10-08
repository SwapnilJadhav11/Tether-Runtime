"""Shared test configuration."""

from __future__ import annotations

import asyncio
import sys

# psycopg's async driver cannot run on Windows' default ProactorEventLoop. Production runs on
# Linux; this only affects tests on the Windows development host.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
