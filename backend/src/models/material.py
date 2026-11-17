from sqlalchemy import String, Double, Boolean, ForeignKey, Integer, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base


class MaterialGate(Base):
    """Single row used as a database-level gate for material mutations and recall scans."""
    __tablename__ = 'material_gate'
    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=0)


class Roll(Base):
    __tablename__ = 'rolls'
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(160), default='')
    note: Mapped[str] = mapped_column(String(500), default='')
    created_at: Mapped[float] = mapped_column(Double)
    recall_ticket_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class RollLink(Base):
    """Directed lineage edge. A child may have several parents (merged batches)."""
    __tablename__ = 'roll_links'
    __table_args__ = (UniqueConstraint('parent_id', 'child_id', name='uq_roll_link'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    parent_id: Mapped[int] = mapped_column(ForeignKey('rolls.id'), index=True)
    child_id: Mapped[int] = mapped_column(ForeignKey('rolls.id'), index=True)
    kind: Mapped[str] = mapped_column(String(20), default='split')
    created_at: Mapped[float] = mapped_column(Double)


class SegmentUsage(Base):
    """A trial segment (dataset record `run`) consumed material from a roll."""
    __tablename__ = 'segment_usages'
    __table_args__ = (UniqueConstraint('roll_id', 'run', name='uq_usage_roll_run'), Index('ix_usage_run', 'run'))
    id: Mapped[int] = mapped_column(primary_key=True)
    roll_id: Mapped[int] = mapped_column(ForeignKey('rolls.id'), index=True)
    run: Mapped[str] = mapped_column(String(120))
    note: Mapped[str] = mapped_column(String(300), default='')
    created_at: Mapped[float] = mapped_column(Double)


class DatasetRun(Base):
    """Indexed projection of dataset records so recall guards never scan JSON blobs."""
    __tablename__ = 'dataset_runs'
    __table_args__ = (UniqueConstraint('dataset_id', 'run', name='uq_dataset_run'), Index('ix_dataset_run_run', 'run'))
    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey('datasets.id'), index=True)
    run: Mapped[str] = mapped_column(String(120))
    shift: Mapped[str] = mapped_column(String(40), default='')


class RecallTicket(Base):
    __tablename__ = 'recall_tickets'
    id: Mapped[int] = mapped_column(primary_key=True)
    roll_id: Mapped[int] = mapped_column(ForeignKey('rolls.id'), unique=True)
    reason: Mapped[str] = mapped_column(String(500), default='')
    status: Mapped[str] = mapped_column(String(20), default='pending')
    created_by: Mapped[str] = mapped_column(String(150), default='')
    created_at: Mapped[float] = mapped_column(Double)
    started_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    finished_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    scanned_rolls: Mapped[int] = mapped_column(Integer, default=0)
    last_batch_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    error: Mapped[str] = mapped_column(String(1000), default='')


class RecallRoll(Base):
    """Per-ticket work queue of discovered rolls, including scan progress."""
    __tablename__ = 'recall_rolls'
    __table_args__ = (UniqueConstraint('ticket_id', 'roll_id', name='uq_recall_roll'), Index('ix_recall_pending', 'ticket_id', 'processed'))
    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey('recall_tickets.id'), index=True)
    roll_id: Mapped[int] = mapped_column(ForeignKey('rolls.id'), index=True)
    depth: Mapped[int] = mapped_column(Integer, default=0)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey('rolls.id'), nullable=True)
    processed: Mapped[bool] = mapped_column(Boolean, default=False)
    discovered_at: Mapped[float] = mapped_column(Double)


class RecallDataset(Base):
    __tablename__ = 'recall_datasets'
    __table_args__ = (UniqueConstraint('ticket_id', 'dataset_id', name='uq_recall_dataset'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey('recall_tickets.id'), index=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey('datasets.id'), index=True)
    runs: Mapped[int] = mapped_column(Integer, default=0)
    at: Mapped[float] = mapped_column(Double)


class RecallJob(Base):
    __tablename__ = 'recall_jobs'
    __table_args__ = (UniqueConstraint('ticket_id', 'job_id', name='uq_recall_job'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey('recall_tickets.id'), index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey('jobs.id'), index=True)
    at: Mapped[float] = mapped_column(Double)


class RecallReport(Base):
    __tablename__ = 'recall_reports'
    __table_args__ = (UniqueConstraint('ticket_id', 'report_id', name='uq_recall_report'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey('recall_tickets.id'), index=True)
    report_id: Mapped[int] = mapped_column(ForeignKey('reports.id'), index=True)
    at: Mapped[float] = mapped_column(Double)


class RecallSegment(Base):
    __tablename__ = 'recall_segments'
    __table_args__ = (UniqueConstraint('ticket_id', 'roll_id', 'run', name='uq_recall_segment'), Index('ix_recall_segment_run', 'ticket_id', 'run'))
    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey('recall_tickets.id'), index=True)
    roll_id: Mapped[int] = mapped_column(ForeignKey('rolls.id'), index=True)
    run: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[float] = mapped_column(Double)
