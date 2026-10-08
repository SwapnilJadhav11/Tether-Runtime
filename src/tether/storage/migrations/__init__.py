"""Alembic migrations for the ``tether`` schema (PD-4).

Alembic manages only the ``tether`` schema; the ``langgraph`` schema belongs to the LangGraph
saver. Migrations run as the ``tether_migrator`` role in the one-shot ``migrate`` process.
"""
