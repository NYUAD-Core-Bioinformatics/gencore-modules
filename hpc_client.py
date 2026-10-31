"""SSH, Slurm, and SFTP operations for the Jubail automation service."""

from __future__ import annotations

import json
import os
import re
import shlex
import tempfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Iterator

import paramiko


JOB_ID_PATTERN = re.compile(r"^[0-9]+$")
REQUEST_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


class HPCError(RuntimeError):
    pass


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


class HPCClient:
    def __init__(self) -> None:
        self.host = os.getenv("HPC_HOST", "jubail.abudhabi.nyu.edu")
        self.port = int(os.getenv("HPC_PORT", "22"))
        self.username = os.getenv("HPC_USERNAME", "gencore")
        self.key_path = os.getenv("HPC_SSH_KEY_PATH", "/run/secrets/hpc_ssh_key")
        self.known_hosts = os.getenv("HPC_KNOWN_HOSTS", "")
        self.allow_unknown_host = env_flag("HPC_ALLOW_UNKNOWN_HOST_KEY", False)
        self.root = PurePosixPath(
            os.getenv(
                "HPC_AUTOMATION_ROOT",
                "/scratch/gencore/GencoreModule_automate",
            )
        )
        self.connect_timeout = int(os.getenv("HPC_CONNECT_TIMEOUT", "10"))
        self.command_timeout = int(os.getenv("HPC_COMMAND_TIMEOUT", "60"))

    @contextmanager
    def connect(self) -> Iterator[paramiko.SSHClient]:
        client = paramiko.SSHClient()
        if self.known_hosts:
            client.load_host_keys(self.known_hosts)
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        elif self.allow_unknown_host:
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        else:
            raise HPCError(
                "HPC host-key verification is not configured. Set HPC_KNOWN_HOSTS "
                "or explicitly set HPC_ALLOW_UNKNOWN_HOST_KEY=true for testing."
            )

        try:
            client.connect(
                hostname=self.host,
                port=self.port,
                username=self.username,
                key_filename=self.key_path,
                look_for_keys=False,
                allow_agent=False,
                timeout=self.connect_timeout,
                banner_timeout=self.connect_timeout,
                auth_timeout=self.connect_timeout,
            )
            transport = client.get_transport()
            if transport:
                transport.set_keepalive(30)
            yield client
        except (OSError, paramiko.SSHException) as error:
            raise HPCError(str(error)) from error
        finally:
            client.close()

    def execute(self, client: paramiko.SSHClient, command: str) -> str:
        try:
            _, stdout, stderr = client.exec_command(
                command,
                timeout=self.command_timeout,
            )
            output = stdout.read().decode("utf-8", errors="replace")
            error = stderr.read().decode("utf-8", errors="replace")
            status = stdout.channel.recv_exit_status()
        except (OSError, paramiko.SSHException) as exception:
            raise HPCError(str(exception)) from exception
        if status != 0:
            detail = error.strip() or output.strip() or f"remote exit status {status}"
            raise HPCError(detail[:4000])
        return output.strip()

    @staticmethod
    def _ensure_remote_directory(sftp: paramiko.SFTPClient, path: PurePosixPath) -> None:
        current = PurePosixPath("/")
        for part in path.parts[1:]:
            current /= part
            try:
                sftp.stat(str(current))
            except FileNotFoundError:
                sftp.mkdir(str(current), mode=0o750)

    def upload_request(self, client: paramiko.SSHClient, payload: dict) -> None:
        request_id = str(payload["request_id"])
        if not REQUEST_ID_PATTERN.fullmatch(request_id):
            raise HPCError("Invalid request UUID")

        request_dir = self.root / "requests" / request_id
        request_path = request_dir / "request.json"
        temporary_path = request_dir / f".request.json.{os.getpid()}.tmp"
        try:
            with client.open_sftp() as sftp:
                self._ensure_remote_directory(sftp, request_dir)
                with sftp.file(str(temporary_path), "w") as stream:
                    stream.write(json.dumps(payload, indent=2) + "\n")
                    stream.flush()
                try:
                    sftp.chmod(str(temporary_path), 0o640)
                    sftp.posix_rename(str(temporary_path), str(request_path))
                except IOError:
                    try:
                        sftp.remove(str(request_path))
                    except FileNotFoundError:
                        pass
                    sftp.rename(str(temporary_path), str(request_path))
        except (OSError, paramiko.SSHException) as error:
            raise HPCError(str(error)) from error

    def submit(self, client: paramiko.SSHClient, request_id: str) -> str:
        if not REQUEST_ID_PATTERN.fullmatch(request_id):
            raise HPCError("Invalid request UUID")
        command = f"{shlex.quote(str(self.root / 'submit_conda_build.sh'))} {shlex.quote(request_id)}"
        output = self.execute(client, command)
        job_id = output.splitlines()[-1].strip() if output else ""
        if not JOB_ID_PATTERN.fullmatch(job_id):
            raise HPCError(f"Unexpected Slurm submission response: {output[:1000]}")
        return job_id

    def job_state(self, client: paramiko.SSHClient, job_id: str) -> tuple[str, str]:
        if not JOB_ID_PATTERN.fullmatch(job_id):
            raise HPCError("Invalid Slurm job ID")
        command = (
            f"sacct -n -P -j {shlex.quote(job_id)} "
            "--format=JobIDRaw,State,ExitCode"
        )
        output = self.execute(client, command)
        for line in output.splitlines():
            fields = line.split("|")
            if len(fields) >= 3 and fields[0].strip() == job_id:
                state = fields[1].strip().split()[0].rstrip("+").upper()
                return state, fields[2].strip()
        return "UNKNOWN", ""

    def read_result(self, client: paramiko.SSHClient, request_id: str) -> dict | None:
        result_path = self.root / "requests" / request_id / "result.json"
        try:
            with client.open_sftp() as sftp:
                with sftp.file(str(result_path), "r") as stream:
                    payload = stream.read()
        except FileNotFoundError:
            return None
        except (OSError, paramiko.SSHException) as error:
            raise HPCError(str(error)) from error
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        try:
            value = json.loads(payload)
        except ValueError as error:
            raise HPCError(f"Invalid remote result.json: {error}") from error
        if not isinstance(value, dict):
            raise HPCError("Remote result.json is not a JSON object")
        return value

    def generate_and_download_catalog(self, client: paramiko.SSHClient) -> Path:
        generator = self.root / "generate_catalog.py"
        catalog = self.root / "catalog" / "packages.json"
        command = (
            f"{shlex.quote(str(generator))} --output {shlex.quote(str(catalog))}"
        )
        self.execute(client, command)

        descriptor, temporary_name = tempfile.mkstemp(
            prefix="gencore-packages-", suffix=".json"
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        try:
            with client.open_sftp() as sftp:
                sftp.get(str(catalog), str(temporary_path))
        except (OSError, paramiko.SSHException) as error:
            temporary_path.unlink(missing_ok=True)
            raise HPCError(str(error)) from error
        return temporary_path
