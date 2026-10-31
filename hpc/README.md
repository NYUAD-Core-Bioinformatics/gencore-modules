# Jubail EasyBuild runner

These files are the non-interactive replacement for selection 1 in
`eb_modules_v3.py`. They are installed on Jubail and invoked through the fixed
`submit_conda_build.sh <request-uuid>` interface.

## HPC installation

The default remote directory is:

```text
/scratch/gencore/GencoreModule_automate
```

Copy the five runtime files into that directory, then restrict write access:

```bash
install -d -m 750 /scratch/gencore/GencoreModule_automate/requests
install -m 750 source_vars.sh build_conda_module.sbatch submit_conda_build.sh \
  /scratch/gencore/GencoreModule_automate/
install -m 750 build_conda_module.py generate_catalog.py \
  /scratch/gencore/GencoreModule_automate/
```

Each request is stored as:

```text
requests/<request-uuid>/request.json
```

Example request:

```json
{
  "request_id": "86bccf8c-8094-4d32-bb80-b135d7b6b908",
  "name": "samtools",
  "version": "latest",
  "channel": "bioconda"
}
```

Submit it from the Jubail login node:

```bash
/scratch/gencore/GencoreModule_automate/submit_conda_build.sh \
  86bccf8c-8094-4d32-bb80-b135d7b6b908
```

The Slurm job name is `<package-name>-<requested-version>` (for example,
`acdc-1.02`). The command prints only the numeric Slurm job ID. Build logs are written under
the request's `logs/` directory as `<package-name>_<job-id>.out` and
`<package-name>_<job-id>.err`. The final machine-readable result is stored in
`result.json`.

## Catalog generation

`generate_catalog.py` scans the modulefiles for Gencore generations 1, 2, and
3 and creates a fresh catalog on Jubail:

```text
/scratch/gencore/GencoreModule_automate/catalog/packages.json
```

Run and validate it manually with:

```bash
/scratch/gencore/GencoreModule_automate/generate_catalog.py
python -m json.tool \
  /scratch/gencore/GencoreModule_automate/catalog/packages.json \
  >/dev/null
```

The generator writes to a temporary file, validates it, and then atomically
replaces `packages.json`. It also writes `packages.meta.json` with the UTC
generation time, package count, and scanned roots.

The legacy interactive `eb_modules_v3.py` also calls this generator after each
successful installation mode. `filter.sh` is no longer required. If the
generator is installed somewhere else, set `GENCORE_CATALOG_GENERATOR` to its
absolute path before starting the legacy script.

In the application workflow, the Celery worker runs this command after a
successful build and downloads the resulting JSON through SFTP over its existing
SSH connection. The worker will validate the downloaded file before atomically
replacing the GUI catalog. Jubail therefore does not need an SSH key for the
application server.

## Application-side SSH credentials

Never copy the private key into the Git repository, Docker image, `.env`, or
HPC scripts. On the Linux application server, store it at:

```text
/data/gencore-modules/secrets/hpc_ssh_key
```

Store the verified host key separately at:

```text
/data/gencore-modules/secrets/hpc_known_hosts
```

Both files must be readable only by root or the account running Docker. The
HPC Celery worker receives the private key read-only. Verify the Jubail host-key fingerprint with the HPC administrators
before adding it to `hpc_known_hosts`.

The corresponding public key belongs in `~gencore/.ssh/authorized_keys` for the
dedicated `gencore` HPC submission account.
