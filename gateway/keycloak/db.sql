-- Keycloak's own role and database in the shared PostgreSQL (make mcp-db; safe to rerun).
-- psql runs it with :pw set to KC_DB_PASSWORD from keycloak.env.
SELECT 'CREATE ROLE keycloak LOGIN' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'keycloak')\gexec
ALTER ROLE keycloak WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'pw';
SELECT 'CREATE DATABASE keycloak OWNER keycloak' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'keycloak')\gexec
-- Each role reaches only its own database (aistudio, the image's superuser, still reaches both).
REVOKE CONNECT, TEMPORARY ON DATABASE keycloak FROM PUBLIC;
REVOKE CONNECT, TEMPORARY ON DATABASE aistudio FROM PUBLIC;
