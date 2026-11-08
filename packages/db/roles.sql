-- psql bootstrap for the isolated Channel360 test roles.
-- Passwords are collected below using psql's hidden \password command. They
-- never enter SQL text, process arguments, shell history or server logs.
-- Role creation and ownership markers are transactional and safe to resume.
SET password_encryption = 'scram-sha-256';

DO $channel360_roles$
DECLARE
    role_name text;
    can_login boolean;
    is_superuser boolean;
    can_create_db boolean;
    can_create_role boolean;
    can_replicate boolean;
    can_bypass_rls boolean;
    ownership_marker text;
BEGIN
    FOREACH role_name IN ARRAY ARRAY[
        'channel360_test_migrator',
        'channel360_test_app',
        'channel360_test_worker'
    ] LOOP
        SELECT r.rolcanlogin, r.rolsuper, r.rolcreatedb, r.rolcreaterole,
               r.rolreplication, r.rolbypassrls,
               coalesce(shobj_description(r.oid, 'pg_authid'), '')
          INTO can_login, is_superuser, can_create_db, can_create_role,
               can_replicate, can_bypass_rls, ownership_marker
          FROM pg_roles AS r
         WHERE r.rolname = role_name;

        IF FOUND THEN
            IF ownership_marker <> 'channel360-local-test-role-v1'
               OR NOT can_login OR is_superuser OR can_create_db OR can_create_role
               OR can_replicate OR can_bypass_rls THEN
                RAISE EXCEPTION 'Refusing to alter unmarked or unsafe role %', role_name;
            END IF;
        ELSE
            EXECUTE format(
                'CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS',
                role_name
            );
            EXECUTE format(
                'COMMENT ON ROLE %I IS %L',
                role_name, 'channel360-local-test-role-v1'
            );
        END IF;
    END LOOP;
END
$channel360_roles$;

\password channel360_test_migrator
\password channel360_test_app
\password channel360_test_worker
