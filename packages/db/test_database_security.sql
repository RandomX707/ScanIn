-- Execute as a PostgreSQL administrator in the dedicated test database only.
-- This revokes public schema rights only in the current database.
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO channel360_test_migrator;
GRANT USAGE ON SCHEMA public TO channel360_test_app;
