"""Celery email and event-driven HPC package automation tasks."""

from __future__ import annotations

import json
import os
from typing import Any

from celery import Celery
from celery.signals import worker_process_init, worker_ready
from flask_mail import Message
from sqlalchemy.exc import OperationalError

from app import app, mail
from catalog import (
    find_highest_installed_package,
    find_installed_package,
    publish_downloaded_catalog,
)
from conda_registry import RegistryUnavailable, resolve_conda_package
from database import (
    PackageRequest,
    Session,
    init_database,
    reset_database_connections_after_fork,
    unfinished_request_ids,
)
from hpc_client import HPCClient, HPCError, env_flag


celery = Celery(
    "gencore_modules",
    broker=os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0"),
    backend=os.getenv("CELERY_RESULT_BACKEND", "redis://redis:6379/1"),
)
celery.conf.update(
    broker_connection_retry_on_startup=True,
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    task_track_started=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_time_limit=int(os.getenv("CELERY_TASK_TIME_LIMIT", "120")),
)

TERMINAL_STATES = {"ALREADY_AVAILABLE", "MANUAL_REVIEW", "COMPLETED", "FAILED"}
ACTIVE_SLURM_STATES = {
    "CONFIGURING",
    "COMPLETING",
    "PENDING",
    "REQUEUED",
    "RESIZING",
    "RUNNING",
    "SUSPENDED",
}


def notification_sender() -> tuple[str, str]:
    return app.config["MAIL_SENDER_NAME"], app.config["MAIL_DEFAULT_SENDER"]


def update_request(request_id: str, **values: Any) -> PackageRequest:
    with Session.begin() as session:
        package_request = session.get(PackageRequest, request_id)
        if package_request is None:
            raise ValueError(f"Unknown request: {request_id}")
        for field, value in values.items():
            setattr(package_request, field, value)
        session.flush()
        session.expunge(package_request)
        return package_request


def load_request(request_id: str) -> PackageRequest:
    with Session() as session:
        package_request = session.get(PackageRequest, request_id)
        if package_request is None:
            raise ValueError(f"Unknown request: {request_id}")
        session.expunge(package_request)
        return package_request


def send_final_message(
    package_request: PackageRequest,
    subject: str,
    body: str,
    notify_admin: bool = False,
) -> None:
    message = Message(
        subject=subject,
        sender=notification_sender(),
        recipients=[package_request.requester_email],
        body=body,
    )
    with app.app_context():
        mail.send(message)

    if notify_admin:
        admin_message = Message(
            subject=subject,
            sender=notification_sender(),
            recipients=[app.config["ADMIN_EMAIL"]],
            cc=app.config["ADMIN_CC_EMAILS"],
            body=body,
        )
        with app.app_context():
            mail.send(admin_message)


@celery.task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_kwargs={"max_retries": 3},
)
def notify_already_available(self, request_id: str, package: dict[str, str]) -> None:
    package_request = load_request(request_id)
    send_final_message(
        package_request,
        f"{package['name']}/{package['version']} Is Already Available",
        f"""Dear {package_request.requester_name},

The requested software is already available on Jubail.

Request ID: {request_id}

module load all gencore/{package['master_version']}
module load {package['name']}/{package['version']}

Regards
NYU Abu Dhabi Core Bioinformatics
""",
    )


@celery.task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_kwargs={"max_retries": 3},
)
def notify_manual_review(self, request_id: str) -> None:
    package_request = load_request(request_id)
    if package_request.status_detail == "HPC automation is disabled":
        explanation = (
            "Automated HPC submission is currently disabled, so this request "
            "has been routed to an administrator."
        )
    else:
        explanation = (
            "The requested package or version is not available in the approved "
            "Bioconda or conda-forge channels, so it was not installed automatically."
        )
    send_final_message(
        package_request,
        f"Your Package Request for {package_request.package_name} Was Received",
        f"""Dear {package_request.requester_name},

We checked the Gencore catalog and the approved Conda channels.

{explanation} An administrator will review the request and contact you when
the manual installation has been handled.

Request ID: {request_id}

Regards
NYU Abu Dhabi Core Bioinformatics
""",
    )


@celery.task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_kwargs={"max_retries": 3},
)
def notify_manual_review_admin(self, request_id: str) -> None:
    package_request = load_request(request_id)
    message = Message(
        subject=f"Manual Package Installation Required: {package_request.package_name}",
        sender=notification_sender(),
        recipients=[app.config["ADMIN_EMAIL"]],
        cc=app.config["ADMIN_CC_EMAILS"],
        reply_to=package_request.requester_email,
    )
    message.body = f"""A package request requires manual administrator review.

Request ID        : {request_id}
Requester         : {package_request.requester_name}
Requester Email   : {package_request.requester_email}
Software Name     : {package_request.package_name}
Requested Version : {package_request.requested_version}
Conda URL         : {package_request.conda_url or 'Not provided'}
GitHub URL        : {package_request.github_url or 'Not provided'}
Database URL      : {package_request.database_url or 'Not provided'}
Reason            : {package_request.status_detail or 'Manual review required'}

The software was not submitted to Slurm automatically.
"""
    with app.app_context():
        mail.send(message)


@celery.task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_kwargs={"max_retries": 3},
)
def notify_build_queued(self, request_id: str) -> None:
    package_request = load_request(request_id)
    send_final_message(
        package_request,
        f"Your Package Request for {package_request.package_name} Was Accepted",
        f"""Dear {package_request.requester_name},

We checked the Gencore catalog and approved Conda channels. The package is
available from {package_request.channel} and has entered the automated Jubail
installation workflow.

Request ID       : {request_id}
Software Name    : {package_request.package_name}
Resolved Version : {package_request.resolved_version}
Slurm Job ID     : {package_request.slurm_job_id}

We will send another email when the installation completes or requires
administrator attention.

Regards
NYU Abu Dhabi Core Bioinformatics
""",
    )


@celery.task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_kwargs={"max_retries": 3},
)
def notify_build_success(self, request_id: str) -> None:
    package_request = load_request(request_id)
    send_final_message(
        package_request,
        f"{package_request.package_name}/{package_request.resolved_version} Is Ready",
        f"""Dear {package_request.requester_name},

Your requested software was installed successfully and is now available on Jubail.

Request ID: {request_id}

module load all gencore/3
module load {package_request.package_name}/{package_request.resolved_version}

Regards
NYU Abu Dhabi Core Bioinformatics
""",
    )


@celery.task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_kwargs={"max_retries": 3},
)
def notify_build_failure(self, request_id: str) -> None:
    package_request = load_request(request_id)
    send_final_message(
        package_request,
        f"Installation Failed for {package_request.package_name}",
        f"""Dear {package_request.requester_name},

The automated installation did not complete successfully. The request has been
flagged for administrator attention; no further action is required from you.

Request ID: {request_id}
Details: {package_request.status_detail or 'See the Jubail build logs.'}

Regards
NYU Abu Dhabi Core Bioinformatics
""",
        notify_admin=True,
    )


@celery.task(
    bind=True,
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_backoff_max=60,
    max_retries=None,
)
def process_package_request(self, request_id: str) -> str:
    package_request = load_request(request_id)
    if package_request.status in TERMINAL_STATES:
        return package_request.status
    if package_request.slurm_job_id:
        monitor_slurm_job.apply_async(args=[request_id], countdown=5)
        return "MONITOR_QUEUED"

    try:
        installed = find_installed_package(
            package_request.package_name,
            package_request.requested_version,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise self.retry(exc=error, countdown=30, max_retries=None)

    if installed:
        update_request(
            request_id,
            status="ALREADY_AVAILABLE",
            resolved_version=installed["version"],
            status_detail=(
                f"Available in Gencore generation {installed['master_version']}"
            ),
        )
        return "ALREADY_AVAILABLE"

    update_request(request_id, status="VALIDATING", status_detail=None)
    try:
        conda_package = resolve_conda_package(
            package_request.package_name,
            package_request.requested_version,
            package_request.conda_url,
        )
    except RegistryUnavailable as error:
        update_request(request_id, status_detail=f"Anaconda API unavailable: {error}")
        raise self.retry(exc=error, countdown=60, max_retries=None)

    if conda_package is not None:
        try:
            installed = find_installed_package(
                package_request.package_name,
                conda_package.version,
            )
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise self.retry(exc=error, countdown=30, max_retries=None)

        if installed:
            update_request(
                request_id,
                status="ALREADY_AVAILABLE",
                channel=conda_package.channel,
                resolved_version=installed["version"],
                status_detail=(
                    "Latest approved-channel version is already available in "
                    f"Gencore generation {installed['master_version']}"
                ),
            )
            return "ALREADY_AVAILABLE"

    if (
        conda_package is None
        and package_request.requested_version.casefold() == "latest"
    ):
        try:
            installed = find_highest_installed_package(package_request.package_name)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise self.retry(exc=error, countdown=30, max_retries=None)
        if installed:
            update_request(
                request_id,
                status="ALREADY_AVAILABLE",
                resolved_version=installed["version"],
                status_detail=(
                    "Package is installed locally and is not listed in the "
                    "approved Conda channels"
                ),
            )
            return "ALREADY_AVAILABLE"

    if conda_package is None or not env_flag("HPC_AUTOMATION_ENABLED", False):
        detail = (
            "Package is not present in Bioconda or conda-forge"
            if conda_package is None
            else "HPC automation is disabled"
        )
        update_request(request_id, status="MANUAL_REVIEW", status_detail=detail)
        notify_manual_review.delay(request_id)
        notify_manual_review_admin.delay(request_id)
        return "MANUAL_REVIEW"

    update_request(
        request_id,
        status="SUBMITTING",
        channel=conda_package.channel,
        resolved_version=conda_package.version,
        status_detail=None,
    )

    hpc = HPCClient()
    try:
        with hpc.connect() as client:
            hpc.upload_request(
                client,
                {
                    "request_id": request_id,
                    "name": conda_package.name,
                    "version": conda_package.version,
                    "channel": conda_package.channel,
                },
            )
            job_id = hpc.submit(client, request_id)
    except HPCError as error:
        update_request(request_id, status_detail=f"HPC submission unavailable: {error}")
        delay = min(1800, 30 * (2 ** min(self.request.retries, 6)))
        raise self.retry(exc=error, countdown=delay, max_retries=None)

    update_request(
        request_id,
        status="SUBMITTED",
        slurm_job_id=job_id,
        status_detail=f"Slurm job {job_id} submitted",
    )
    notify_build_queued.delay(request_id)
    monitor_slurm_job.apply_async(args=[request_id], countdown=10)
    return job_id


@celery.task(
    bind=True,
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_backoff_max=60,
    max_retries=None,
)
def monitor_slurm_job(self, request_id: str) -> str:
    package_request = load_request(request_id)
    if package_request.status in TERMINAL_STATES:
        return package_request.status
    if not package_request.slurm_job_id:
        process_package_request.delay(request_id)
        return "SUBMISSION_QUEUED"

    hpc = HPCClient()
    try:
        with hpc.connect() as client:
            state, exit_code = hpc.job_state(client, package_request.slurm_job_id)
            result = hpc.read_result(client, request_id)

            if state in ACTIVE_SLURM_STATES or (
                state == "UNKNOWN"
                and (not result or result.get("status") == "building")
            ):
                display_state = "RUNNING" if state == "RUNNING" else "SUBMITTED"
                update_request(
                    request_id,
                    status=display_state,
                    status_detail=f"Slurm state: {state}",
                )
                monitor_slurm_job.apply_async(
                    args=[request_id],
                    countdown=int(os.getenv("HPC_POLL_INTERVAL", "60")),
                )
                return state

            if (
                state in {"COMPLETED", "UNKNOWN"}
                and result
                and result.get("status") == "completed"
            ):
                update_request(
                    request_id,
                    status="CATALOG_SYNC_PENDING",
                    status_detail="Build completed; refreshing the GUI catalog",
                    result_json=json.dumps(result, sort_keys=True),
                )
                downloaded = hpc.generate_and_download_catalog(client)
                count = publish_downloaded_catalog(
                    downloaded,
                    package_request.package_name,
                    str(result.get("version") or package_request.resolved_version),
                )
                update_request(
                    request_id,
                    status="COMPLETED",
                    resolved_version=str(
                        result.get("version") or package_request.resolved_version
                    ),
                    status_detail=f"Installed; GUI catalog now contains {count} records",
                )
                notify_build_success.delay(request_id)
                return "COMPLETED"

            detail = "Slurm state: {} (exit code {})".format(state, exit_code or "unknown")
            if result and result.get("error"):
                detail = f"{detail}; {result['error']}"
            update_request(
                request_id,
                status="FAILED",
                status_detail=detail[:4000],
                result_json=json.dumps(result, sort_keys=True) if result else None,
            )
            notify_build_failure.delay(request_id)
            return "FAILED"
    except (HPCError, OSError, ValueError, json.JSONDecodeError) as error:
        update_request(request_id, status_detail=f"HPC monitoring retry: {error}")
        delay = min(1800, 30 * (2 ** min(self.request.retries, 6)))
        raise self.retry(exc=error, countdown=delay, max_retries=None)


@worker_ready.connect
def recover_unfinished_requests(**_: Any) -> None:
    if not env_flag("HPC_RECOVERY_ENABLED", False):
        return
    init_database()
    for request_id, job_id in unfinished_request_ids():
        if job_id:
            monitor_slurm_job.delay(request_id)
        else:
            process_package_request.delay(request_id)


@worker_process_init.connect
def initialize_worker_process_database(**_: Any) -> None:
    reset_database_connections_after_fork()
    init_database()
