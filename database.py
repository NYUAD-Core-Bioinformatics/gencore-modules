"""Durable package-request state shared by Flask and Celery."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    String,
    Text,
    create_engine,
    select,
    text,
    update,
)
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
    monitoring_alert_sent: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=text("false"),
    )
    next_monitor_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
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

        # create_all does not add columns to an existing table. Keep this
        # small migration here until the project adopts a migration tool.
        # Existing MONITORING_ATTENTION rows have already generated an alert,
        # so backfill them as sent when the column is first introduced.
        if engine.dialect.name == "postgresql":
            column_exists = connection.execute(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = current_schema()
                          AND table_name = 'package_requests'
                          AND column_name = 'monitoring_alert_sent'
                    )
                    """
                )
            ).scalar_one()
            if not column_exists:
                connection.execute(
                    text(
                        """
                        ALTER TABLE package_requests
                        ADD COLUMN monitoring_alert_sent BOOLEAN
                        NOT NULL DEFAULT FALSE
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        UPDATE package_requests
                        SET monitoring_alert_sent = TRUE
                        WHERE status = 'MONITORING_ATTENTION'
                        """
                    )
                )

            next_monitor_column_exists = connection.execute(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = current_schema()
                          AND table_name = 'package_requests'
                          AND column_name = 'next_monitor_at'
                    )
                    """
                )
            ).scalar_one()
            if not next_monitor_column_exists:
                connection.execute(
                    text(
                        """
                        ALTER TABLE package_requests
                        ADD COLUMN next_monitor_at TIMESTAMP WITH TIME ZONE NULL
                        """
                    )
                )
        elif engine.dialect.name == "sqlite":
            columns = {
                row[1]
                for row in connection.execute(
                    text("PRAGMA table_info(package_requests)")
                )
            }
            if "monitoring_alert_sent" not in columns:
                connection.execute(
                    text(
                        """
                        ALTER TABLE package_requests
                        ADD COLUMN monitoring_alert_sent BOOLEAN
                        NOT NULL DEFAULT 0
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        UPDATE package_requests
                        SET monitoring_alert_sent = 1
                        WHERE status = 'MONITORING_ATTENTION'
                        """
                    )
                )
            if "next_monitor_at" not in columns:
                connection.execute(
                    text(
                        """
                        ALTER TABLE package_requests
                        ADD COLUMN next_monitor_at DATETIME NULL
                        """
                    )
                )


def reset_database_connections_after_fork() -> None:
    """Drop connections inherited from a pre-fork Celery parent process."""
    engine.dispose(close=False)


def get_request(request_id: str) -> PackageRequest | None:
    with Session() as session:
        return session.get(PackageRequest, request_id)


def claim_monitoring_alert(request_id: str) -> bool:
    """Atomically claim the one monitoring alert allowed for a request."""
    with Session.begin() as session:
        result = session.execute(
            update(PackageRequest)
            .where(
                PackageRequest.request_id == request_id,
                PackageRequest.monitoring_alert_sent.is_(False),
            )
            .values(monitoring_alert_sent=True)
        )
        return result.rowcount == 1


def release_monitoring_alert(request_id: str) -> None:
    """Release a claim when SMTP fails so Celery can retry the alert."""
    with Session.begin() as session:
        session.execute(
            update(PackageRequest)
            .where(PackageRequest.request_id == request_id)
            .values(monitoring_alert_sent=False)
        )


def claim_monitor_poll(request_id: str, interval_seconds: int) -> bool:
    """Atomically allow one HPC poll for a request during an interval."""
    now = datetime.now(timezone.utc)
    next_monitor_at = now + timedelta(seconds=interval_seconds)
    with Session.begin() as session:
        result = session.execute(
            update(PackageRequest)
            .where(
                PackageRequest.request_id == request_id,
                (
                    PackageRequest.next_monitor_at.is_(None)
                    | (PackageRequest.next_monitor_at <= now)
                ),
            )
            .values(next_monitor_at=next_monitor_at)
        )
        return result.rowcount == 1


def defer_monitor_poll(request_id: str, delay_seconds: int) -> None:
    """Prevent duplicate monitors from polling before a retry is due."""
    next_monitor_at = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
    with Session.begin() as session:
        session.execute(
            update(PackageRequest)
            .where(PackageRequest.request_id == request_id)
            .values(next_monitor_at=next_monitor_at)
        )


def unfinished_request_ids() -> list[tuple[str, str | None, datetime | None]]:
    terminal = {"ALREADY_AVAILABLE", "MANUAL_REVIEW", "COMPLETED", "FAILED"}
    with Session() as session:
        rows = session.execute(
            select(
                PackageRequest.request_id,
                PackageRequest.slurm_job_id,
                PackageRequest.next_monitor_at,
            ).where(PackageRequest.status.not_in(terminal))
        )
        return list(rows.tuples())
