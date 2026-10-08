#!/bin/sh
# Postgres first-boot initialisation (runs once, on an empty data volume).
#
# Creates the Tether and demo-target databases, the `tether` and `langgraph` schemas, and the
# two Tether roles (architecture §18/§19, PD-4):
#   tether_migrator  owns the schemas and runs DDL (the one-shot migrate service)
#   tether_app       used by api and worker: DML only, no DDL anywhere
# Role passwords come from the environment (deploy/compose/.env), never from the repository.
set -eu

: "${TETHER_MIGRATOR_DB_PASSWORD:?TETHER_MIGRATOR_DB_PASSWORD must be set}"
: "${TETHER_APP_DB_PASSWORD:?TETHER_APP_DB_PASSWORD must be set}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
    -v migrator_password="$TETHER_MIGRATOR_DB_PASSWORD" \
    -v app_password="$TETHER_APP_DB_PASSWORD" <<'SQL'
CREATE ROLE tether_migrator LOGIN PASSWORD :'migrator_password';
CREATE ROLE tether_app LOGIN PASSWORD :'app_password';

CREATE DATABASE tether OWNER tether_migrator;
-- Placeholder for the demo target service; its own roles and schema arrive in S2.5.
CREATE DATABASE demo_target;

-- Nobody but the owner may create schemas or temporary tables in the Tether database.
REVOKE ALL ON DATABASE tether FROM PUBLIC;
GRANT CONNECT ON DATABASE tether TO tether_app;

\connect tether

REVOKE ALL ON SCHEMA public FROM PUBLIC;

-- Alembic manages `tether`; the LangGraph saver manages `langgraph` (from S1.3). Both run as
-- the migrator in the migrate service.
CREATE SCHEMA tether AUTHORIZATION tether_migrator;
CREATE SCHEMA langgraph AUTHORIZATION tether_migrator;

-- The application role can use both schemas but cannot create objects in them.
GRANT USAGE ON SCHEMA tether, langgraph TO tether_app;

-- Tables and sequences the migrator creates later are usable by the application role.
-- Narrower grants (e.g. append-only audit tables) are applied by the migrations that need them.
ALTER DEFAULT PRIVILEGES FOR ROLE tether_migrator IN SCHEMA tether, langgraph
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO tether_app;
ALTER DEFAULT PRIVILEGES FOR ROLE tether_migrator IN SCHEMA tether, langgraph
    GRANT USAGE, SELECT ON SEQUENCES TO tether_app;

ALTER ROLE tether_migrator IN DATABASE tether SET search_path = tether;
ALTER ROLE tether_app IN DATABASE tether SET search_path = tether;
SQL
