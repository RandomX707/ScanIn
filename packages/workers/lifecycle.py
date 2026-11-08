from __future__ import annotations

from uuid import UUID

from sqlalchemy import Connection, text

from packages.audit.chain import append_event
from packages.db.tenant import require_tenant_context
from packages.db.worker_context import ValidatedJobContext


class WorkerLifecycleRepository:
    """Narrow document-state writer for the trusted orchestration worker role."""

    def __init__(self, connection: Connection, job: ValidatedJobContext):
        if require_tenant_context(connection) != job.tenant_id:
            raise PermissionError("worker tenant context does not match validated job")
        self.connection = connection
        self.job = job

    def transition(self, document_id: UUID, *, outcome: str, actor_id: str) -> str:
        row = self.connection.execute(text("""SELECT tenant_id,ingestion_state FROM documents
            WHERE tenant_id=:tenant AND id=:document FOR UPDATE"""),
            {"tenant": self.job.document_tenant_id, "document": document_id}).one_or_none()
        if row is None or UUID(str(row[0])) != self.job.document_tenant_id:
            raise PermissionError("worker job does not own the target document")
        state = str(row[1])
        if outcome == "begin":
            transitions = {
                "staged_private": ("scan_pending", "scanning", "scan_clean", "inspecting"),
                "scan_pending": ("scanning", "scan_clean", "inspecting"),
                "scanning": ("scan_clean", "inspecting"),
                "scan_clean": ("inspecting",),
            }
            next_states = transitions.get(state, ())
            if state in {"inspecting", "ready"}:
                return state
            if not next_states:
                raise ValueError("document_not_inspectable")
            for next_state in next_states:
                self._set_state(document_id, next_state, actor_id)
            return "inspecting"
        if outcome not in {"ready", "failed"}:
            raise ValueError("invalid_inspection_outcome")
        if outcome == "ready" and state == "ready":
            return state
        if state != "inspecting":
            raise ValueError("document_not_inspecting")
        self._set_state(document_id, outcome, actor_id)
        return outcome

    def _set_state(self, document_id: UUID, state: str, actor_id: str) -> None:
        self.connection.execute(text("""UPDATE documents SET ingestion_state=:state
            WHERE tenant_id=:tenant AND id=:document"""),
            {"state": state, "tenant": self.job.document_tenant_id, "document": document_id})
        append_event(connection=self.connection, actor_id=actor_id, action="document.state_changed",
                     subject=str(document_id), payload={"state": state})
