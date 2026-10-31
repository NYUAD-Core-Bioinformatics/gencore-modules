"""Durable package-request state shared by Flask and Celery."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, DateTime, String, Text, create_engine, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite:////tmp/gencore-modules.db",
)


class Base(DeclarativeBase):
    pass


class PackageRequest(Base):
    __tablename__ = "package_requests"

    request_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    requester_name: Mapped[str] = mapped_column(String(255))
    requester_email: Mapped[str] = mapped_column(String(320))
    package_name: Mapped[str] = mapped_column(String(128), index=True)
    requested_version: Mapped[str] = mapped_column(String(128))
    resolved_version: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    channel: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    conda_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    github_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    database_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True, default="RECEIVED")
    slurm_job_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status_detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    result_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    final_notification_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=300,
    pool_use_lifo=True,
)
Session = sessionmaker(bind=engine, expire_on_commit=False)


def init_database() -> None:
    # Gunicorn and Celery may start several processes at the same time. On a
    # new PostgreSQL database, create_all's individual existence checks are
    # otherwise vulnerable to a race where two processes both try to create
    # the same table. A transaction-scoped advisory lock serializes startup
    # schema initialization without leaving any persistent lock state.
    with engine.begin() as connection:
        if engine.dialect.name == "postgresql":
            connection.execute(
                text("SELECT pg_advisory_xact_lock(:lock_id)"),
                {"lock_id": 6746562636673718631},
            )
        Base.metadata.create_all(connection)


def reset_database_connections_after_fork() -> None:
    """Drop connections inherited from a pre-fork Celery parent process."""
    engine.dispose(close=False)


def get_request(request_id: str) -> PackageRequest | None:
    with Session() as session:
        return session.get(PackageRequest, request_id)


def unfinished_request_ids() -> list[tuple[str, str | None]]:
    terminal = {"ALREADY_AVAILABLE", "MANUAL_REVIEW", "COMPLETED", "FAILED"}
    with Session() as session:
        rows = session.execute(
            select(PackageRequest.request_id, PackageRequest.slurm_job_id).where(
                PackageRequest.status.not_in(terminal)
            )
        )
        return list(rows.tuples())
