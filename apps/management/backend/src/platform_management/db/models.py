from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Spec(Base):
    __tablename__ = "specifications"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, index=True)
    resource_id: Mapped[str] = mapped_column(String)
    current_revision: Mapped[int] = mapped_column(Integer, default=0)
    draft: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)


class Revision(Base):
    __tablename__ = "revisions"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    spec_key: Mapped[str] = mapped_column(ForeignKey("specifications.key"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    document: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("spec_key", "revision"),)


class ExecutionBinding(Base):
    __tablename__ = "execution_bindings"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    payload: Mapped[dict] = mapped_column(JSONB)


class Resource(Base):
    __tablename__ = "resources"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, index=True)
    payload: Mapped[dict] = mapped_column(JSONB)
    binding_id: Mapped[str | None] = mapped_column(
        ForeignKey("execution_bindings.id"), nullable=True
    )
    active_operation_id: Mapped[str | None] = mapped_column(String, nullable=True)


class RunRow(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    binding_id: Mapped[str | None] = mapped_column(
        ForeignKey("execution_bindings.id"), nullable=True
    )
    snapshot: Mapped[dict] = mapped_column(JSONB)
    payload: Mapped[dict] = mapped_column(JSONB)
    context: Mapped[dict] = mapped_column(JSONB, default=dict)


class Operation(Base):
    __tablename__ = "operations"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    target_id: Mapped[str] = mapped_column(String, index=True)
    payload: Mapped[dict] = mapped_column(JSONB)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    operation_id: Mapped[str | None] = mapped_column(ForeignKey("operations.id"), nullable=True)
    binding_id: Mapped[str | None] = mapped_column(
        ForeignKey("execution_bindings.id"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String, default="operation")
    state: Mapped[str] = mapped_column(String, default="pending")
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    owner: Mapped[str | None] = mapped_column(String, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    checkpoint: Mapped[dict] = mapped_column(JSONB, default=dict)
    __table_args__ = (Index("jobs_due", "state", "due_at"),)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    operation_id: Mapped[str] = mapped_column(ForeignKey("operations.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    message: Mapped[str] = mapped_column(String)


class Submission(Base):
    __tablename__ = "submissions"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String)
    operation_id: Mapped[str] = mapped_column(ForeignKey("operations.id"))


class ProviderIntent(Base):
    __tablename__ = "provider_intents"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    group_id: Mapped[str] = mapped_column(String, index=True)
    state: Mapped[str] = mapped_column(String, default="pending")
    payload: Mapped[dict] = mapped_column(JSONB)


class SimInstance(Base):
    __tablename__ = "simulated_instances"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    intent_id: Mapped[str] = mapped_column(String, index=True)
    group_id: Mapped[str] = mapped_column(String, index=True)
    state: Mapped[str] = mapped_column(String)
    allocated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Observation(Base):
    __tablename__ = "observations"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    payload: Mapped[dict] = mapped_column(JSONB)
    next_refresh: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SessionRow(Base):
    __tablename__ = "sessions"
    token_hash: Mapped[str] = mapped_column(String, primary_key=True)
    csrf_token: Mapped[str] = mapped_column(String)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LoginAttempt(Base):
    __tablename__ = "login_attempts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    address: Mapped[str] = mapped_column(String, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Preference(Base):
    __tablename__ = "preferences"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class ImportReceipt(Base):
    __tablename__ = "import_receipts"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String)
    submission_key: Mapped[str] = mapped_column(String, unique=True)
    fingerprint: Mapped[str] = mapped_column(String)
    document: Mapped[dict] = mapped_column(JSONB)
    source: Mapped[str] = mapped_column(String)
    provenance: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="queued")
    result: Mapped[dict] = mapped_column(JSONB, default=dict)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class InventorySnapshot(Base):
    __tablename__ = "inventory_snapshots"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    cluster_id: Mapped[str] = mapped_column(String, index=True)
    digest: Mapped[str] = mapped_column(String)
    document: Mapped[dict] = mapped_column(JSONB)
    complete: Mapped[bool]
    fixture: Mapped[bool]
    source: Mapped[str] = mapped_column(String)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PreparedRun(Base):
    __tablename__ = "prepared_runs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    digest: Mapped[str] = mapped_column(String)
    snapshot: Mapped[dict] = mapped_column(JSONB)
    fixture: Mapped[bool]
    source: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ArtifactCheck(Base):
    __tablename__ = "artifact_checks"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    uri: Mapped[str] = mapped_column(String)
    sha256: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
