-- Execute against the migrated target database as its migration/table-owner role.
-- Required psql variables: migration_role, application_role.
\if :{?migration_role}
\else
  \echo 'Missing required psql variable: migration_role'
  \quit 2
\endif
\if :{?application_role}
\else
  \echo 'Missing required psql variable: application_role'
  \quit 2
\endif
\if :{?worker_role}
\else
  \echo 'Missing required worker_role'
  \quit 2
\endif
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO :"application_role";
GRANT USAGE ON SCHEMA public TO :"worker_role";
GRANT SELECT, INSERT ON users, files, document_versions TO :"application_role";
GRANT SELECT, INSERT ON documents TO :"application_role";
GRANT UPDATE (current_version_id, document_category) ON documents TO :"application_role";
-- State advancement is reserved for the later isolated ingestion worker role.
GRANT SELECT, INSERT ON audit_events TO :"application_role";
GRANT SELECT ON tenants TO :"application_role";
GRANT UPDATE (audit_sequence, audit_head_hash) ON tenants TO :"application_role";
GRANT SELECT, INSERT, UPDATE ON fields TO :"application_role";
GRANT SELECT, INSERT ON data_corrections TO :"application_role";
GRANT SELECT, INSERT, UPDATE ON edit_sets TO :"application_role";
GRANT SELECT, INSERT, UPDATE, DELETE ON patches TO :"application_role";
-- Historical review rows are retained as evidence but new approvals are not
-- part of the single-owner workflow. Keep them readable and non-writable.
GRANT SELECT ON reviews TO :"application_role";
REVOKE INSERT, UPDATE, DELETE ON reviews FROM :"application_role";
GRANT SELECT, INSERT ON verification_manifests TO :"application_role";
-- The trusted orchestration worker may advance only the ingestion state. It
-- never receives owner, superuser, or RLS-bypass attributes.
GRANT SELECT ON documents, files, document_versions TO :"worker_role";
GRANT UPDATE (ingestion_state) ON documents TO :"worker_role";
GRANT SELECT, INSERT ON audit_events TO :"worker_role";
GRANT SELECT ON tenants TO :"worker_role";
GRANT UPDATE (audit_sequence, audit_head_hash) ON tenants TO :"worker_role";
ALTER DEFAULT PRIVILEGES FOR ROLE :"migration_role" IN SCHEMA public REVOKE ALL ON TABLES FROM PUBLIC;
