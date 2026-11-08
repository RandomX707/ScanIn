from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import Connection, text

from packages.audit.chain import append_event
from packages.db.tenant import require_tenant_context
from packages.storage.local import StoredFile


@dataclass(frozen=True)
class RegisteredDocument:
    tenant_id: UUID
    document_id: UUID
    version_id: UUID
    file_id: UUID
    filename: str
    source_sha256: str
    size_bytes: int
    storage_key: str


class DocumentRepository:
    """Tenant-filtered immutable original/version registration."""

    def __init__(self, connection: Connection, tenant_id: UUID):
        if require_tenant_context(connection) != tenant_id:
            raise PermissionError("repository tenant does not match transaction context")
        self.connection = connection
        self.tenant_id = tenant_id

    def register_clean_original(self, *, filename: str, media_type: str,
                                stored: StoredFile, actor_id: str) -> RegisteredDocument:
        if stored.tenant_id != self.tenant_id:
            raise PermissionError("stored file tenant does not match transaction context")
        if not filename or len(filename) > 255 or "\x00" in filename:
            raise ValueError("invalid_document_filename")
        if not media_type or len(media_type) > 255 or "\x00" in media_type:
            raise ValueError("invalid_detected_media_type")
        # The constraint is deferred to commit so the document can point at its
        # source-version UUID while all four records are inserted atomically.
        document_id, version_id, file_id = stored.document_id, uuid4(), uuid4()
        self.connection.execute(text("""INSERT INTO documents
            (id,tenant_id,filename,ingestion_state,current_version_id)
            VALUES (:id,:tenant,:filename,'staged_private',:version)"""),
            {"id": document_id, "tenant": self.tenant_id, "filename": filename, "version": version_id})
        self.connection.execute(text("""INSERT INTO files
            (id,tenant_id,sha256,size_bytes,mime,storage_key,file_role)
            VALUES (:id,:tenant,:sha256,:size,:mime,:key,'original')"""),
            {"id": file_id, "tenant": self.tenant_id, "sha256": stored.sha256,
             "size": stored.size_bytes, "mime": media_type, "key": stored.key})
        self.connection.execute(text("""INSERT INTO document_versions
            (id,tenant_id,document_id,kind,file_id,sha256)
            VALUES (:version,:tenant,:document,'original',:file,:sha256)"""),
            {"version": version_id, "tenant": self.tenant_id, "document": document_id,
             "file": file_id, "sha256": stored.sha256})
        append_event(connection=self.connection, actor_id=actor_id, action="document.original_registered",
                     subject=str(document_id), payload={"sha256": stored.sha256, "size_bytes": stored.size_bytes})
        return RegisteredDocument(self.tenant_id, document_id, version_id, file_id,
                                  filename, stored.sha256, stored.size_bytes, stored.key)

    def get(self, document_id: UUID) -> dict[str, object] | None:
        row = self.connection.execute(text("""SELECT d.id,d.tenant_id,d.filename,d.document_category,d.ingestion_state,
            d.current_version_id,v.sha256,f.size_bytes,f.storage_key,f.mime
            FROM documents AS d LEFT JOIN document_versions AS v
              ON v.tenant_id=d.tenant_id AND v.id=d.current_version_id
            LEFT JOIN files AS f ON f.tenant_id=v.tenant_id AND f.id=v.file_id
            WHERE d.tenant_id=:tenant AND d.id=:id"""),
            {"tenant": self.tenant_id, "id": document_id}).mappings().one_or_none()
        return dict(row) if row is not None else None

    def list_recent(self, *, limit: int = 50) -> list[dict[str, object]]:
        if not 1 <= limit <= 100:
            raise ValueError("limit_out_of_range")
        rows = self.connection.execute(text("""SELECT d.id,d.tenant_id,d.filename,d.document_category,d.ingestion_state,
            d.current_version_id,d.created_at,
            (SELECT count(*) FROM document_versions v WHERE v.tenant_id=d.tenant_id AND v.document_id=d.id) AS version_count,
            (SELECT v.kind FROM document_versions v WHERE v.tenant_id=d.tenant_id AND v.id=d.current_version_id) AS current_version_kind
            FROM documents d WHERE d.tenant_id=:tenant
            ORDER BY created_at DESC,id LIMIT :limit"""),
            {"tenant": self.tenant_id, "limit": limit}).mappings()
        return [dict(row) for row in rows]

    def dashboard_snapshot(self) -> dict[str, object]:
        metrics = self.connection.execute(text("""SELECT
            (SELECT count(*) FROM documents WHERE tenant_id=:tenant) AS total_documents,
            (SELECT count(*) FROM documents WHERE tenant_id=:tenant AND ingestion_state IN
                ('staged_private','scan_pending','scanning','scan_clean','inspecting')) AS awaiting_inspection,
            (SELECT count(DISTINCT f.document_id) FROM data_corrections c
                JOIN fields f ON f.tenant_id=c.tenant_id AND f.id=c.field_id
                WHERE c.tenant_id=:tenant) AS documents_with_corrections,
            (SELECT count(*) FROM edit_sets WHERE tenant_id=:tenant AND status='submitted') AS pending_approvals,
            (SELECT count(*) FROM verification_manifests WHERE tenant_id=:tenant AND outcome='VERIFIED') AS verified_versions"""),
            {"tenant": self.tenant_id}).mappings().one()
        activity = self.connection.execute(text("""SELECT sequence,ts,actor_id,action,subject
            FROM audit_events WHERE tenant_id=:tenant ORDER BY sequence DESC LIMIT 8"""),
            {"tenant": self.tenant_id}).mappings()
        return {
            "metrics": dict(metrics),
            "recent_documents": self.list_recent(limit=8),
            "recent_activity": [dict(row) for row in activity],
        }

    def get_original(self, document_id: UUID) -> dict[str, object] | None:
        row = self.connection.execute(text("""SELECT d.id AS document_id,d.tenant_id,d.filename,d.ingestion_state,
            v.id AS version_id,v.sha256,f.size_bytes,f.storage_key,f.mime
            FROM documents d JOIN document_versions v ON v.tenant_id=d.tenant_id AND v.document_id=d.id
            JOIN files f ON f.tenant_id=v.tenant_id AND f.id=v.file_id
            WHERE d.tenant_id=:tenant AND d.id=:document AND v.kind='original'"""),
            {"tenant": self.tenant_id, "document": document_id}).mappings().one_or_none()
        return dict(row) if row is not None else None

    def get_version(self, document_id: UUID, version_id: UUID) -> dict[str, object] | None:
        row = self.connection.execute(text("""SELECT d.id AS document_id,d.tenant_id,d.filename,
            d.ingestion_state,v.id AS version_id,v.parent_version_id,v.kind,v.sha256,
            f.size_bytes,f.storage_key,f.mime,f.file_role
            FROM documents AS d JOIN document_versions AS v
              ON v.tenant_id=d.tenant_id AND v.document_id=d.id
            JOIN files AS f ON f.tenant_id=v.tenant_id AND f.id=v.file_id
            WHERE d.tenant_id=:tenant AND d.id=:document AND v.id=:version"""),
            {"tenant": self.tenant_id, "document": document_id, "version": version_id}).mappings().one_or_none()
        return dict(row) if row is not None else None

    def list_versions(self, document_id: UUID) -> list[dict[str, object]]:
        rows = self.connection.execute(text("""SELECT v.id AS version_id,v.parent_version_id,v.kind,
            v.sha256,v.created_at,f.size_bytes,f.mime,d.current_version_id=(v.id) AS is_current
            ,e.id AS edit_set_id,e.status AS edit_set_status,m.outcome AS verification_outcome
            FROM document_versions AS v JOIN documents AS d
              ON d.tenant_id=v.tenant_id AND d.id=v.document_id
            JOIN files AS f ON f.tenant_id=v.tenant_id AND f.id=v.file_id
            LEFT JOIN edit_sets AS e ON e.tenant_id=v.tenant_id AND e.id=v.edit_set_id
            LEFT JOIN LATERAL (SELECT outcome FROM verification_manifests vm
              WHERE vm.tenant_id=e.tenant_id AND vm.edit_set_id=e.id ORDER BY vm.created_at DESC LIMIT 1) m ON true
            WHERE v.tenant_id=:tenant AND v.document_id=:document
            ORDER BY v.created_at,v.id"""),
            {"tenant": self.tenant_id, "document": document_id}).mappings()
        return [dict(row) for row in rows]

    def set_document_category(self, document_id: UUID, *, category: str, actor_id: str) -> None:
        result = self.connection.execute(text("""UPDATE documents SET document_category=:category
            WHERE tenant_id=:tenant AND id=:document AND NOT EXISTS
              (SELECT 1 FROM edit_sets WHERE tenant_id=:tenant AND document_id=:document)
            RETURNING id"""), {"tenant": self.tenant_id, "document": document_id, "category": category})
        if result.scalar_one_or_none() is None:
            raise ValueError("document_missing_or_already_has_edit_sets")
        append_event(connection=self.connection, actor_id=actor_id, action="document.category_classified",
                     subject=str(document_id), payload={"category": category})

    def create_edit_set(self, *, edit_set_id: UUID, document_id: UUID, base_version_id: UUID,
                        category: str, actor_id: str, reason: str) -> None:
        result = self.connection.execute(text("""INSERT INTO edit_sets
            (id,tenant_id,document_id,base_version_id,authorized_category,created_by,reason)
            SELECT :id,:tenant,:document,:version,:category,:actor,:reason
            WHERE EXISTS (SELECT 1 FROM documents WHERE tenant_id=:tenant AND id=:document
                          AND current_version_id=:version AND ingestion_state='ready'
                          AND document_category=:category)
            RETURNING id"""), {"id": edit_set_id, "tenant": self.tenant_id, "document": document_id,
                "version": base_version_id, "category": category, "actor": actor_id, "reason": reason.strip()})
        if result.scalar_one_or_none() is None:
            raise ValueError("document_not_authorized_or_version_stale")
        append_event(connection=self.connection, actor_id=actor_id, action="edit_set.draft_created",
                     subject=str(edit_set_id), payload={"document_id": str(document_id), "reason": reason.strip()})

    def get_edit_set(self, edit_set_id: UUID) -> dict[str, object] | None:
        row = self.connection.execute(text("""SELECT e.id,e.tenant_id,e.document_id,e.base_version_id,
            e.status,e.frozen_hash,e.authorized_category,e.created_by,e.reason,e.patch_plan,e.adapter,
            e.adapter_version,e.released_version_id,d.current_version_id,d.document_category,d.filename
            FROM edit_sets e JOIN documents d ON d.tenant_id=e.tenant_id AND d.id=e.document_id
            WHERE e.tenant_id=:tenant AND e.id=:edit_set"""),
            {"tenant": self.tenant_id, "edit_set": edit_set_id}).mappings().one_or_none()
        return dict(row) if row is not None else None

    def list_edit_sets(self, document_id: UUID) -> list[dict[str, object]]:
        rows = self.connection.execute(text("""SELECT id AS edit_set_id,base_version_id,status,frozen_hash,
            authorized_category,created_by,reason,patch_plan,adapter,adapter_version,released_version_id,
            created_at,updated_at FROM edit_sets WHERE tenant_id=:tenant AND document_id=:document
            ORDER BY created_at DESC,id DESC"""),
            {"tenant": self.tenant_id, "document": document_id}).mappings()
        return [dict(row) for row in rows]

    def list_document_audit(self, document_id: UUID, *, limit: int = 100) -> list[dict[str, object]]:
        if not 1 <= limit <= 500:
            raise ValueError("limit_out_of_range")
        rows = self.connection.execute(text("""SELECT id,sequence,ts,actor_id,action,subject,payload,event_hash
            FROM audit_events WHERE tenant_id=:tenant AND (
              subject=:document_text OR payload->>'document_id'=:document_text OR
              subject IN (SELECT id::text FROM edit_sets WHERE tenant_id=:tenant AND document_id=:document_uuid) OR
              subject IN (SELECT id::text FROM fields WHERE tenant_id=:tenant AND document_id=:document_uuid)
            ) ORDER BY sequence DESC LIMIT :limit"""),
            {"tenant": self.tenant_id, "document_text": str(document_id),
             "document_uuid": document_id, "limit": limit}).mappings()
        return [dict(row) for row in rows]

    def list_patches(self, edit_set_id: UUID) -> list[dict[str, object]]:
        rows = self.connection.execute(text("""SELECT id,target_id,expected_old_value,expected_fingerprint,new_value
            FROM patches WHERE tenant_id=:tenant AND edit_set_id=:edit_set ORDER BY created_at,id"""),
            {"tenant": self.tenant_id, "edit_set": edit_set_id}).mappings()
        return [dict(row) for row in rows]

    def add_patches(self, edit_set_id: UUID, patches: list[dict[str, object]], *, actor_id: str) -> None:
        if not patches:
            raise ValueError("patch_set_empty")
        status = self.connection.execute(text("SELECT status FROM edit_sets WHERE tenant_id=:tenant AND id=:id FOR UPDATE"),
                                         {"tenant": self.tenant_id, "id": edit_set_id}).scalar_one_or_none()
        if status != "draft":
            raise ValueError("edit_set_not_draft")
        for patch in patches:
            self.connection.execute(text("""INSERT INTO patches
                (tenant_id,edit_set_id,target_id,expected_old_value,expected_fingerprint,new_value)
                VALUES (:tenant,:edit,:target,CAST(:old AS jsonb),:fingerprint,CAST(:new AS jsonb))"""),
                {"tenant": self.tenant_id, "edit": edit_set_id, "target": patch["target_id"],
                 "old": json.dumps(patch["expected_old_value"], sort_keys=True),
                 "fingerprint": patch["expected_fingerprint"],
                 "new": json.dumps(patch["new_value"], sort_keys=True)})
        append_event(connection=self.connection, actor_id=actor_id, action="edit_set.targets_added",
                     subject=str(edit_set_id), payload={"target_ids": sorted(str(patch["target_id"]) for patch in patches),
                     "patch_count": len(patches)})

    def freeze_edit_set(self, edit_set_id: UUID, *, frozen_hash: str, plan: dict[str, object],
                        adapter: str, adapter_version: str, actor_id: str) -> None:
        result = self.connection.execute(text("""UPDATE edit_sets SET status='submitted',frozen_hash=:hash,
            patch_plan=CAST(:plan AS jsonb),adapter=:adapter,adapter_version=:version,updated_at=now()
            WHERE tenant_id=:tenant AND id=:id AND status='draft' RETURNING id"""),
            {"tenant": self.tenant_id, "id": edit_set_id, "hash": frozen_hash,
             "plan": json.dumps(plan, sort_keys=True, separators=(",", ":")),
             "adapter": adapter, "version": adapter_version})
        if result.scalar_one_or_none() is None:
            raise ValueError("edit_set_not_draft")
        append_event(connection=self.connection, actor_id=actor_id, action="edit_set.frozen_and_submitted",
                     subject=str(edit_set_id), payload={"frozen_hash": frozen_hash, "adapter": adapter})

    def review_edit_set(self, edit_set_id: UUID, *, decision: str, reviewer_id: str,
                        frozen_hash: str, note: str = "") -> None:
        row = self.connection.execute(text("""SELECT status,created_by,frozen_hash FROM edit_sets
            WHERE tenant_id=:tenant AND id=:id FOR UPDATE"""),
            {"tenant": self.tenant_id, "id": edit_set_id}).one_or_none()
        if row is None or row[0] != "submitted" or str(row[2]).strip() != frozen_hash:
            raise ValueError("edit_set_review_precondition_failed")
        if row[1] == reviewer_id:
            raise ValueError("reviewer_must_differ_from_author")
        if decision not in {"approved", "rejected"}:
            raise ValueError("invalid_review_decision")
        self.connection.execute(text("""INSERT INTO reviews
            (tenant_id,edit_set_id,edit_set_hash,reviewer_id,decision,note)
            VALUES (:tenant,:edit,:hash,:reviewer,:decision,:note)"""),
            {"tenant": self.tenant_id, "edit": edit_set_id, "hash": frozen_hash,
             "reviewer": reviewer_id, "decision": decision, "note": note.strip() or None})
        self.connection.execute(text("UPDATE edit_sets SET status=:status,updated_at=now() WHERE tenant_id=:tenant AND id=:id"),
                                {"status": decision, "tenant": self.tenant_id, "id": edit_set_id})
        append_event(connection=self.connection, actor_id=reviewer_id, action=f"edit_set.{decision}",
                     subject=str(edit_set_id), payload={"frozen_hash": frozen_hash})

    def commit_derived_release(self, *, edit_set_id: UUID, stored: StoredFile,
                               outcome: str, adapter: str, adapter_version: str,
                               manifest: dict[str, object], actor_id: str) -> UUID:
        """Atomically attach a verifier report and a derived version to an approved edit set."""
        if outcome not in {"VERIFIED", "INCOMPLETE"}:
            raise ValueError("candidate_outcome_not_releasable")
        if stored.tenant_id != self.tenant_id:
            raise PermissionError("stored file tenant does not match transaction context")
        row = self.connection.execute(text("""SELECT e.status,e.released_version_id,e.document_id,
            e.base_version_id,d.current_version_id,d.filename
            FROM edit_sets e JOIN documents d ON d.tenant_id=e.tenant_id AND d.id=e.document_id
            WHERE e.tenant_id=:tenant AND e.id=:edit_set FOR UPDATE OF e,d"""),
            {"tenant": self.tenant_id, "edit_set": edit_set_id}).one_or_none()
        if row is None:
            raise ValueError("edit_set_not_found")
        status, released_version_id, document_id, base_version_id, current_version_id, filename = row
        if status == "released" and released_version_id is not None:
            return released_version_id
        if status != "approved":
            raise ValueError("edit_set_not_approved")
        if base_version_id != current_version_id:
            raise ValueError("stale_base_version")
        if stored.document_id != document_id:
            raise PermissionError("stored file document does not match edit set")

        file_id, version_id = uuid4(), uuid4()
        self.connection.execute(text("""INSERT INTO files
            (id,tenant_id,sha256,size_bytes,mime,storage_key,file_role)
            VALUES (:id,:tenant,:sha256,:size,:mime,:key,'derived')"""),
            {"id": file_id, "tenant": self.tenant_id, "sha256": stored.sha256,
             "size": stored.size_bytes, "mime": manifest.get("media_type", "application/octet-stream"),
             "key": stored.key})
        self.connection.execute(text("""INSERT INTO document_versions
            (id,tenant_id,document_id,parent_version_id,file_id,kind,sha256,edit_set_id)
            VALUES (:id,:tenant,:document,:parent,:file,'derived',:sha256,:edit_set)"""),
            {"id": version_id, "tenant": self.tenant_id, "document": document_id,
             "parent": base_version_id, "file": file_id, "sha256": stored.sha256, "edit_set": edit_set_id})
        self.connection.execute(text("""INSERT INTO verification_manifests
            (tenant_id,edit_set_id,source_sha256,output_sha256,outcome,adapter,adapter_version,manifest)
            VALUES (:tenant,:edit,:source,:output,:outcome,:adapter,:version,CAST(:manifest AS jsonb))"""),
            {"tenant": self.tenant_id, "edit": edit_set_id, "source": str(manifest["source_sha256"]),
             "output": stored.sha256, "outcome": outcome, "adapter": adapter, "version": adapter_version,
             "manifest": json.dumps(manifest, sort_keys=True, separators=(",", ":"))})
        updated = self.connection.execute(text("""UPDATE documents SET current_version_id=:version
            WHERE tenant_id=:tenant AND id=:document AND current_version_id=:base RETURNING id"""),
            {"version": version_id, "tenant": self.tenant_id, "document": document_id,
             "base": base_version_id}).scalar_one_or_none()
        if updated is None:
            raise ValueError("stale_base_version")
        self.connection.execute(text("""UPDATE edit_sets SET status='released',released_version_id=:version,updated_at=now()
            WHERE tenant_id=:tenant AND id=:edit_set AND status='approved'"""),
            {"version": version_id, "tenant": self.tenant_id, "edit_set": edit_set_id})
        append_event(connection=self.connection, actor_id=actor_id, action="document.derived_version_released",
                     subject=str(document_id), payload={"edit_set_id": str(edit_set_id),
                     "version_id": str(version_id), "source_sha256": str(manifest["source_sha256"]),
                     "output_sha256": stored.sha256, "outcome": outcome})
        return version_id

    def record_blocked_release(self, *, edit_set_id: UUID, outcome: str, adapter: str,
                               adapter_version: str, source_sha256: str,
                               manifest: dict[str, object], actor_id: str) -> None:
        if outcome != "BLOCKED":
            raise ValueError("blocked_manifest_outcome_required")
        status = self.connection.execute(text("""SELECT status FROM edit_sets
            WHERE tenant_id=:tenant AND id=:edit_set FOR UPDATE"""),
            {"tenant": self.tenant_id, "edit_set": edit_set_id}).scalar_one_or_none()
        if status != "approved":
            raise ValueError("edit_set_not_approved")
        self.connection.execute(text("""INSERT INTO verification_manifests
            (tenant_id,edit_set_id,source_sha256,output_sha256,outcome,adapter,adapter_version,manifest)
            VALUES (:tenant,:edit,:source,NULL,'BLOCKED',:adapter,:version,CAST(:manifest AS jsonb))"""),
            {"tenant": self.tenant_id, "edit": edit_set_id, "source": source_sha256,
             "adapter": adapter, "version": adapter_version,
             "manifest": json.dumps(manifest, sort_keys=True, separators=(",", ":"))})
        self.connection.execute(text("UPDATE edit_sets SET status='failed',updated_at=now() WHERE tenant_id=:tenant AND id=:edit"),
                                {"tenant": self.tenant_id, "edit": edit_set_id})
        append_event(connection=self.connection, actor_id=actor_id, action="document.derived_release_blocked",
                     subject=str(edit_set_id), payload={"source_sha256": source_sha256, "outcome": outcome})

    def create_manual_field(self, *, document_id: UUID, version_id: UUID, target_id: str,
                            fingerprint: str, label: str, observed_value: dict[str, object],
                            actor_id: str) -> UUID:
        if not target_id or not fingerprint or len(fingerprint) != 64 or not label.strip():
            raise ValueError("invalid_manual_field")
        result = self.connection.execute(text("""INSERT INTO fields
            (tenant_id,document_id,version_id,label,source,observed_value,target_id,
             target_fingerprint,created_by)
            SELECT :tenant,:document,:version,:label,'manual',CAST(:observed AS jsonb),:target,
                   :fingerprint,:actor
            WHERE EXISTS (SELECT 1 FROM documents WHERE tenant_id=:tenant AND id=:document
                          AND current_version_id=:version)
            RETURNING id"""),
            {"tenant": self.tenant_id, "document": document_id, "version": version_id,
             "label": label[:255], "observed": json.dumps(observed_value, sort_keys=True),
             "target": target_id, "fingerprint": fingerprint, "actor": actor_id})
        field_id = result.scalar_one_or_none()
        if field_id is None:
            raise ValueError("document_version_changed")
        append_event(connection=self.connection, actor_id=actor_id, action="field.manual_created",
                     subject=str(field_id), payload={"document_id": str(document_id), "target_id": target_id})
        return field_id

    def list_fields(self, document_id: UUID) -> list[dict[str, object]]:
        rows = self.connection.execute(text("""SELECT id AS field_id,document_id,version_id,label,source,
            observed_value,target_id,target_fingerprint,evidence,review_status,created_by,created_at
            FROM fields WHERE tenant_id=:tenant AND document_id=:document
            ORDER BY created_at,id"""), {"tenant": self.tenant_id, "document": document_id}).mappings()
        return [dict(row) for row in rows]

    def append_data_correction(self, *, document_id: UUID, field_id: UUID, value: dict[str, object],
                               reason: str, actor_id: str) -> UUID:
        if not reason.strip() or len(reason) > 2000:
            raise ValueError("correction_reason_required")
        result = self.connection.execute(text("""INSERT INTO data_corrections
            (tenant_id,field_id,source_version_id,corrected_value,reason,actor_id)
            SELECT :tenant,f.id,f.version_id,CAST(:value AS jsonb),:reason,:actor
            FROM fields AS f WHERE f.tenant_id=:tenant AND f.document_id=:document AND f.id=:field
            RETURNING id"""),
            {"tenant": self.tenant_id, "document": document_id, "field": field_id,
             "value": json.dumps(value, sort_keys=True), "reason": reason.strip(), "actor": actor_id})
        correction_id = result.scalar_one_or_none()
        if correction_id is None:
            raise ValueError("field_not_found_for_document")
        append_event(connection=self.connection, actor_id=actor_id, action="field.data_corrected",
                     subject=str(field_id), payload={"correction_id": str(correction_id), "reason": reason.strip()})
        return correction_id


    def list_data_corrections(self, document_id: UUID) -> list[dict[str, object]]:
        rows = self.connection.execute(text("""SELECT c.id,c.field_id,c.source_version_id,c.corrected_value,
            c.reason,c.actor_id,c.created_at,f.label,f.observed_value
            FROM data_corrections AS c JOIN fields AS f
              ON f.tenant_id=c.tenant_id AND f.id=c.field_id
            WHERE c.tenant_id=:tenant AND f.document_id=:document
            ORDER BY c.created_at,c.id"""),
            {"tenant": self.tenant_id, "document": document_id}).mappings()
        return [dict(row) for row in rows]
