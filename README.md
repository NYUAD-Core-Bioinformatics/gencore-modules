# Gencore Modules

Gencore Modules is an authenticated web application for discovering software
modules on the NYU Abu Dhabi Jubail HPC and requesting new Conda-based module
builds. Requests for packages available in Bioconda or conda-forge can be
validated, submitted to Slurm, monitored, published to the catalog, and
reported to the requester without blocking the web request.

## Contents

- [Architecture](#architecture)
- [Request workflow](#request-workflow)
- [Requirements](#requirements)
- [Configuration](#configuration)
- [HPC installation](#hpc-installation)
- [Application deployment](#application-deployment)
- [Validation](#validation)
- [Operations](#operations)
- [Backup and recovery](#backup-and-recovery)
- [Troubleshooting](#troubleshooting)
- [Local development](#local-development)
- [Repository layout](#repository-layout)
- [Security](#security)

## Architecture

The production stack contains six services:

| Service | Responsibility |
| --- | --- |
| Nginx | Terminates HTTPS on port 443 and forwards requests to OAuth2 Proxy. |
| OAuth2 Proxy | Authenticates users with Microsoft Entra OpenID Connect and stores sessions in Redis. |
| Flask/Gunicorn | Serves the catalog, request form, package pages, and request-status API. |
| Celery worker | Validates packages, submits and monitors Slurm jobs, updates the catalog, and sends email. |
| Redis | Stores Celery messages/results and OAuth2 Proxy sessions. |
| PostgreSQL | Stores durable package-request state used for retries and outage recovery. |

Only Nginx publishes a host port. PostgreSQL, Redis, Gunicorn, and OAuth2 Proxy
remain on private Compose networks.

## Request workflow

1. The web application checks `catalog/packages.json` for an installed module.
2. An exact installed version redirects immediately to its package page.
3. A missing package is checked against Bioconda and conda-forge.
4. Packages outside the approved channels are routed to manual review; no Slurm
   job is submitted.
5. Eligible packages are uploaded through SFTP and submitted through an
   idempotent wrapper on Jubail.
6. Celery schedules short `sacct` checks until the Slurm job reaches a terminal
   state. A worker is not held open while the build runs.
7. After a successful build, Jubail regenerates the catalog. The worker
   downloads, validates, and atomically publishes the new file.
8. The requester receives loading instructions, or a failure notice when
   administrator attention is required.

There is no Celery Beat service and no continuous catalog polling. On worker
startup, unfinished PostgreSQL records are requeued. The remote request
directory retains its Slurm job ID, preventing duplicate submission after a
retry or application-server outage.

## Requirements

### Application server

- Linux host with Docker Engine and the Docker Compose plugin
- TCP port 443 available
- Valid TLS certificate and matching private key
- Outbound HTTPS access to Microsoft Entra and Anaconda APIs
- Outbound SMTP access
- SSH access to `jubail.abudhabi.nyu.edu` on port 22
- DNS record, or a temporary client-side DNS override, for the application name

### Jubail

- Slurm commands available to the automation account
- EasyBuild and Environment Modules configured by `hpc/source_vars.sh`
- Write permission to the configured automation and EasyBuild directories
- Read/write permission to generate the module catalog

## Configuration

Copy the example file and edit the local copy:

```bash
cp .env.example .env
chmod 600 .env
```

`.env` is ignored by Git. Do not insert Markdown links, quotes, or multiple
variables on the same line. All email identities are supplied through `.env`;
the Python source contains no personal fallback addresses.

### Application and TLS

```dotenv
IMAGE_TAG=local
WEB_WORKERS=2
WEB_THREADS=4
TZ=Asia/Dubai

APP_DOMAIN=gencore-modules.abudhabi.nyu.edu
APP_SERVER_IP=10.230.14.42
HTTPS_PORT=443
TLS_CERT_ON_HOST=./secrets/gencore-modules.crt
TLS_KEY_ON_HOST=./secrets/gencore-modules.key
```

The certificate file must contain the server certificate followed by required
intermediate certificates. The private key must match the certificate and must
not be encrypted with an interactive passphrase.

```bash
install -d -m 700 secrets
install -m 640 /path/to/fullchain.crt secrets/gencore-modules.crt
install -m 600 /path/to/private.key secrets/gencore-modules.key
```

### Microsoft Entra OpenID Connect

The redirect URI registered in Entra must match exactly:

```text
https://gencore-modules.abudhabi.nyu.edu/oauth2/callback
```

OAuth2 Proxy reads its client secret and cookie-encryption secret from ignored
host files:

```bash
umask 077
read -r -s -p "Entra client secret: " OIDC_CLIENT_SECRET
printf '%s' "$OIDC_CLIENT_SECRET" > secrets/entra_client_secret
unset OIDC_CLIENT_SECRET
printf '\n'

dd if=/dev/urandom \
  of=secrets/oauth2_cookie_secret \
  bs=32 count=1 status=none

chown 65532:65532 \
  secrets/entra_client_secret \
  secrets/oauth2_cookie_secret
chmod 600 \
  secrets/entra_client_secret \
  secrets/oauth2_cookie_secret
```

UID/GID `65532` is used by the official OAuth2 Proxy image. The cookie secret
is required even when Redis stores server-side sessions: it protects the
browser's session-reference cookie.

```dotenv
OAUTH2_CLIENT_SECRET_ON_HOST=./secrets/entra_client_secret
OAUTH2_COOKIE_SECRET_ON_HOST=./secrets/oauth2_cookie_secret
EDGE_NETWORK_SUBNET=172.30.42.0/24
```

Change `EDGE_NETWORK_SUBNET` if it overlaps an existing host, VPN, or
institutional network. Tenant, client ID, issuer, redirect URI, and scopes are
configured in `conf/oauth2-proxy.cfg`.

### SMTP and notification routing

The following variables are required. The application exits at startup with a
clear error if a required value is missing. Set exactly one of
`MAIL_USE_TLS` and `MAIL_USE_SSL` to `true`.

```dotenv
MAIL_SERVER=smtp.gmail.com
MAIL_PORT=465
MAIL_USE_TLS=false
MAIL_USE_SSL=true
MAIL_USERNAME=service-account@nyu.edu
MAIL_PASSWORD=application-specific-password
MAIL_DEFAULT_SENDER=service-account@nyu.edu
MAIL_SENDER_NAME=Gencore Module Notification

ADMIN_EMAIL=primary-admin@nyu.edu
ADMIN_CC_EMAILS=second-admin@nyu.edu,third-admin@nyu.edu
```

`ADMIN_CC_EMAILS` is optional and accepts comma-separated plain email
addresses. For Google SMTP, use an application password or an authentication
method approved by the institution; do not use a normal interactive password.

### PostgreSQL, Redis, and Celery

```dotenv
CELERY_BROKER_URL=redis://redis:6379/0
CELERY_RESULT_BACKEND=redis://redis:6379/1
CELERY_CONCURRENCY=2
CELERY_TASK_TIME_LIMIT=600

POSTGRES_DB=gencore_modules
POSTGRES_USER=gencore_modules
POSTGRES_PASSWORD=replace-with-a-long-random-password
DATABASE_URL=postgresql+psycopg://gencore_modules:replace-with-a-long-random-password@postgres:5432/gencore_modules
```

The password embedded in `DATABASE_URL` must match `POSTGRES_PASSWORD`. Redis
database 0 is the Celery broker, database 1 stores Celery results, and database
2 stores OAuth2 Proxy sessions.

### Jubail automation

```dotenv
HPC_AUTOMATION_ENABLED=true
HPC_RECOVERY_ENABLED=true
HPC_HOST=jubail.abudhabi.nyu.edu
HPC_PORT=22
HPC_USERNAME=gencore
HPC_SSH_KEY_ON_HOST=./secrets/hpc_ssh_key
HPC_SSH_KEY_PATH=/run/secrets/hpc_ssh_key
HPC_AUTOMATION_ROOT=/scratch/gencore/GencoreModule_automate
HPC_CONNECT_TIMEOUT=10
HPC_COMMAND_TIMEOUT=180
HPC_POLL_INTERVAL=300
HPC_KNOWN_HOSTS=
HPC_ALLOW_UNKNOWN_HOST_KEY=true

CATALOG_PATH=/app/catalog/packages.json
```

Store the private key outside Git and make it readable by the worker's
UID/GID `10001`:

```bash
install -m 600 /path/to/hpc_ssh_key secrets/hpc_ssh_key
chown 10001:10001 secrets/hpc_ssh_key
```

`HPC_ALLOW_UNKNOWN_HOST_KEY=true` matches the current controlled setup, but it
does not provide strict host identity verification. For production, verify the
Jubail host-key fingerprint with the HPC team, mount a `known_hosts` file into
the worker, set `HPC_KNOWN_HOSTS` to its container path, and change
`HPC_ALLOW_UNKNOWN_HOST_KEY` to `false`.

## HPC installation

Copy the runtime scripts to Jubail:

```bash
scp -i secrets/hpc_ssh_key -P 22 \
  hpc/source_vars.sh \
  hpc/build_conda_module.py \
  hpc/build_conda_module.sbatch \
  hpc/submit_conda_build.sh \
  hpc/generate_catalog.py \
  gencore@jubail.abudhabi.nyu.edu:/scratch/gencore/GencoreModule_automate/
```

On Jubail, apply restricted permissions and create runtime directories:

```bash
cd /scratch/gencore/GencoreModule_automate
chmod 750 source_vars.sh build_conda_module.sbatch submit_conda_build.sh
chmod 750 build_conda_module.py generate_catalog.py
install -d -m 750 requests catalog
```

Review paths, EasyBuild settings, Slurm partition, memory, CPU, and time limits
before production use. Detailed remote request layout and manual test commands
are documented in [`hpc/README.md`](hpc/README.md).

Generate and validate the initial catalog:

```bash
/scratch/gencore/GencoreModule_automate/generate_catalog.py
python -m json.tool \
  /scratch/gencore/GencoreModule_automate/catalog/packages.json \
  >/dev/null
```

## Application deployment

Give the worker atomic-write permission on the host catalog directory:

```bash
chown -R 10001:10001 catalog
chmod 750 catalog
chmod 640 catalog/packages.json
```

Validate the resolved Compose configuration before starting:

```bash
docker compose config
docker compose build --pull
docker compose up -d
docker compose ps
```

The application is then available at:

```text
https://gencore-modules.abudhabi.nyu.edu/
```

If production DNS still points elsewhere during testing, add this mapping to
the **browser client's** `/etc/hosts`, or configure an internal DNS override:

```text
10.230.14.42 gencore-modules.abudhabi.nyu.edu
```

Docker `extra_hosts` affects containers only; it cannot change name resolution
for a browser running outside Docker.

## Validation

### Service health

```bash
docker compose ps
docker compose exec worker \
  celery -A tasks:celery inspect ping --timeout=5
```

Test HTTPS while forcing a local lookup:

```bash
curl --fail --resolve \
  gencore-modules.abudhabi.nyu.edu:443:127.0.0.1 \
  https://gencore-modules.abudhabi.nyu.edu/healthz
```

Expected response:

```json
{"status":"ok"}
```

### SMTP connectivity

Verify the network and authentication path from the worker container, not only
from the Docker host:

```bash
docker compose exec worker python -c \
"import os,smtplib,ssl; s=smtplib.SMTP_SSL(os.environ['MAIL_SERVER'],int(os.environ['MAIL_PORT']),timeout=20,context=ssl.create_default_context()); s.login(os.environ['MAIL_USERNAME'],os.environ['MAIL_PASSWORD']); print('SMTP authentication succeeded'); s.quit()"
```

### End-to-end package request

Use the GUI to test these three cases:

1. An already installed package redirects to its catalog page without sending
   an unnecessary request email.
2. A package absent from Bioconda and conda-forge enters manual review and
   sends the detailed administrator notification.
3. An approved package absent from the local catalog submits one Slurm job,
   reaches a terminal state, updates the catalog, and sends the final result.

Follow the worker during the test:

```bash
docker compose logs -f worker
```

## Operations

### Logs

```bash
docker compose logs -f proxy oauth2-proxy web worker postgres redis
docker compose logs --since=30m worker
docker compose logs --since=30m worker | \
  grep -E 'notify_build_success|notify_build_failure|retry|ERROR'
```

Compose rotates JSON logs at 10 MB and retains three files per service.

### Safe restart and upgrade

Recreating one application service does not require taking down the whole
stack:

```bash
docker compose up -d --build --no-deps web
docker compose up -d --build --no-deps worker
```

For a complete planned restart:

```bash
docker compose down
docker compose up -d
```

PostgreSQL and Redis data persist in named Docker volumes. Unfinished requests
are recovered from PostgreSQL when the worker becomes ready.

### Request status

For an authenticated browser session, request status is available at:

```text
https://gencore-modules.abudhabi.nyu.edu/requests/<request-uuid>
```

Relevant states include `RECEIVED`, `VALIDATING`, `SUBMITTED`, `PENDING`,
`RUNNING`, `ALREADY_AVAILABLE`, `MANUAL_REVIEW`, `COMPLETED`, and `FAILED`.

### Catalog publication

The host `catalog/` directory is mounted read-only in the web container and
read-write in the worker. The worker downloads a new catalog to a temporary
file, validates its schema and expected module, and replaces the live file
atomically. Flask reads it for every request, so no web restart is required.

## Backup and recovery

Back up these items according to institutional policy:

- PostgreSQL database or the `postgres_data` volume
- `catalog/packages.json`
- TLS certificate and private key
- OAuth2 Proxy and HPC secret files
- `.env`
- Jubail automation request records and generated catalog

Never commit backup archives containing credentials.

Example PostgreSQL logical backup:

```bash
docker compose exec -T postgres sh -c \
  'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' \
  > gencore-modules.sql
```

Protect the resulting file and store it outside the repository. Test restore
procedures before relying on backups.

## Troubleshooting

### Application fails at startup

Run `docker compose config` and inspect the service logs. Missing required SMTP
or administrator variables produce an explicit `Required environment variable
... is not set` error.

### No notification email

Check the Celery task result and SMTP connectivity:

```bash
docker compose logs --since=30m worker | \
  grep -E 'notify_|SMTP|retry|ERROR|TimeLimitExceeded'
```

`Task ... received` only confirms queue delivery. `Task ... succeeded` confirms
that the mail task returned successfully.

### SSH authentication or key permission failure

Confirm that the worker sees the expected key path and that host permissions
allow UID `10001` to read it:

```bash
docker compose exec worker sh -c \
  'id; ls -l "$HPC_SSH_KEY_PATH"'
```

### Slurm job does not progress

Check the job and its request-specific logs on Jubail:

```bash
sacct -j <job-id> --format=JobID,JobName,State,ExitCode,Elapsed
squeue -j <job-id>
```

Remote output and error files are stored under:

```text
/scratch/gencore/GencoreModule_automate/requests/<request-uuid>/logs/
```

### Catalog download permission denied

The worker must own or have write permission on the host `catalog/` directory,
not only on the existing JSON file, because publication uses a temporary file
and atomic rename.

### Immediate catalog check falls back to the summary page

A transient Anaconda API failure can cause the web request to queue validation.
The Celery worker retries and redirects the status page if the requested module
is already available. Check both web and worker logs before treating this as a
catalog failure.

## Local development

The easiest full development environment is Docker Compose with a development
`.env`. To run Flask directly, export all required variables first:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
set -a
. ./.env
set +a
flask --app app run --debug
```

Run basic source and catalog checks before committing:

```bash
PYTHONPYCACHEPREFIX=/tmp/gencore-pycache \
  python3 -m compileall -q \
  app.py catalog.py conda_registry.py database.py hpc_client.py tasks.py hpc/*.py
python3 -m json.tool catalog/packages.json >/dev/null
git diff --check
```

## Repository layout

```text
.
├── app.py                    Flask routes, forms, and mail configuration
├── tasks.py                  Celery request, Slurm, catalog, and email tasks
├── database.py               PostgreSQL model and connection lifecycle
├── catalog.py                Catalog lookup, validation, and publication
├── conda_registry.py         Bioconda/conda-forge resolution
├── hpc_client.py             SSH/SFTP and Slurm client
├── templates/                Jinja templates
├── static/                   CSS and image assets
├── catalog/packages.json     Published module catalog
├── hpc/                      Jubail build and catalog scripts
├── conf/oauth2-proxy.cfg     OpenID Connect proxy configuration
├── nginx/gencore.conf        TLS reverse-proxy configuration
├── docker-compose.yml        Production service topology
├── Dockerfile                Shared web/worker image
├── .env.example              Non-secret configuration template
└── requirements.txt          Python dependencies
```

## Security

- Never commit `.env`, `secrets/`, private keys, SMTP passwords, OAuth client
  secrets, session secrets, certificates containing private keys, or database
  dumps.
- Rotate any secret exposed in chat, command history, logs, screenshots, or Git
  history before deployment.
- Keep Redis, PostgreSQL, Gunicorn, and OAuth2 Proxy unpublished from the host.
- Use a verified SSH `known_hosts` file in production.
- Prefer a dedicated, restricted HPC service account over a personal account.
- Restrict repository access if deployment hostnames, internal IP addresses,
  HPC paths, or operational details are institutionally sensitive.
- Review `git status`, `git diff --check`, and the complete Git history before
  pushing to a new or public repository.

This repository is intended for authorized NYU Abu Dhabi operational use.
Choose and add an institutionally approved `LICENSE` file before making the
repository public.
