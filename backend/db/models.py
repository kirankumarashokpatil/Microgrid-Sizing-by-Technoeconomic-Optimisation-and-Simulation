"""
ORM models for the DIP web-app persistence layer.

Design notes
------------
The optimiser engine (``core/``) is stateless — it takes a request + a profiles
DataFrame and returns a result. This layer records the *durable* facts around a
run so the UI (and audits) can retrieve them later:

  projects   — who/where, plus a saved default config (light).
  profiles   — a reusable time-series dataset stored as ONE JSONB row (the load
               + pv + wind per-unit arrays), reconstructed to a DataFrame at run
               time. Never queried by timestamp in SQL, so a normalised per-step
               table would be pure overhead.
  scenarios  — a read-model of the code registry (S00…S123) so the API can serve
               /scenarios from the DB and FK-validate a run's scenario_id.
  runs       — one execution: the full request and the full result, both JSONB.
               The result shape varies by answer_mode (point/curve/surface/…), so
               JSONB keeps it future-proof — a new scenario type needs no DDL.
  run_flows  — the per-timestep FlowsFrame, only for forward_eval scenarios that
               emit one; kept off runs.result so the hot summary row stays small.
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Double, ForeignKey, Identity, Integer,
    String, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base

# ── Run status constants ──────────────────────────────────────────────────────
RUN_PENDING = "pending"
RUN_RUNNING = "running"
RUN_COMPLETED = "completed"
RUN_FAILED = "failed"


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("user_oid", "project_name", name="uq_projects_user_name"),
    )

    project_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_oid: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    project_name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    latitude: Mapped[float | None] = mapped_column(Double, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Double, nullable=True)
    area_ha: Mapped[float | None] = mapped_column(Double, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    runs: Mapped[list["Run"]] = relationship(back_populates="project", cascade="all, delete-orphan")


class Profile(Base):
    __tablename__ = "profiles"

    profile_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str | None] = mapped_column(String(512), nullable=True)
    dt_hours: Mapped[float] = mapped_column(Double, nullable=False)
    n_steps: Mapped[int] = mapped_column(Integer, nullable=False)
    start_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    load_nameplate_mw: Mapped[float | None] = mapped_column(Double, nullable=True)
    pv_nameplate_mw: Mapped[float | None] = mapped_column(Double, nullable=True)
    # {"load_mw": [...], "pv_pu": [...], "wind_pu": [...]}  — one row, expand at runtime.
    series: Mapped[dict] = mapped_column(JSONB, nullable=False)
    meta: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Scenario(Base):
    __tablename__ = "scenarios"

    scenario_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    stage: Mapped[str | None] = mapped_column(String(16), nullable=True)
    topology: Mapped[str | None] = mapped_column(String(32), nullable=True)
    family: Mapped[str | None] = mapped_column(String(16), nullable=True)
    answer_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    needs: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Run(Base):
    __tablename__ = "runs"

    run_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.project_id", ondelete="CASCADE"), nullable=False, index=True
    )
    profile_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("profiles.profile_id", ondelete="SET NULL"), nullable=True
    )
    scenario_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("scenarios.scenario_id"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=RUN_PENDING, index=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    dt_hours: Mapped[float | None] = mapped_column(Double, nullable=True)
    # The full RunRequest, verbatim, so a run is exactly reproducible.
    request: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # The full ScenarioResult JSON (design + kpis + table + optional economics).
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    project: Mapped["Project"] = relationship(back_populates="runs")
    flows: Mapped["RunFlows | None"] = relationship(
        back_populates="run", cascade="all, delete-orphan", uselist=False
    )


class RunFlows(Base):
    __tablename__ = "run_flows"

    run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("runs.run_id", ondelete="CASCADE"), primary_key=True
    )
    n_steps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # {"columns": [...], "rows": [[...], ...]} — the per-timestep energy-flows table.
    series: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    run: Mapped["Run"] = relationship(back_populates="flows")
