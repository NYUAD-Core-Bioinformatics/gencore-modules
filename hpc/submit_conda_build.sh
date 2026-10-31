#!/usr/bin/env bash

set -Eeuo pipefail

if [[ $# -ne 1 ]]; then
    echo "Usage: $0 <request-uuid>" >&2
    exit 64
fi

request_id="$1"
if [[ ! "${request_id}" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$ ]]; then
    echo "Invalid request UUID" >&2
    exit 65
fi

automation_root="${GENCORE_AUTOMATION_ROOT:-/scratch/gencore/GencoreModule_automate}"
python_bin="${GENCORE_PYTHON:-/share/apps/NYUAD5/miniconda/3-4.11.0/envs/python3.11/bin/python}"
request_dir="${automation_root}/requests/${request_id}"
request_file="${request_dir}/request.json"
log_dir="${request_dir}/logs"
job_id_file="${request_dir}/job_id"
submit_lock="${request_dir}/submit.lock"

if [[ ! -f "${request_file}" ]]; then
    echo "Request file does not exist: ${request_file}" >&2
    exit 66
fi

if [[ ! -x "${python_bin}" ]]; then
    echo "Python interpreter is not executable: ${python_bin}" >&2
    exit 69
fi

read -r package_name package_version < <("${python_bin}" -c '
import json
import re
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    request = json.load(handle)

name = str(request["name"]).strip()
version = str(request.get("version") or "latest").strip()

if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name):
    raise SystemExit("Invalid Conda package name")
if version.casefold() != "latest" and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}", version):
    raise SystemExit("Invalid Conda package version")

print(name, version)
' "${request_file}")

job_name="${package_name}-${package_version}"
# Keep room below common Slurm job-name limits while retaining name and version.
job_name="${job_name:0:100}"

mkdir -p "${log_dir}"

# Multiple Celery deliveries for the same request must never create multiple
# Slurm jobs. The lock and durable job_id file make submission idempotent.
exec 9>"${submit_lock}"
flock -x 9

if [[ -f "${job_id_file}" ]]; then
    existing_job_id="$(<"${job_id_file}")"
    if [[ "${existing_job_id}" =~ ^[0-9]+$ ]]; then
        printf '%s\n' "${existing_job_id}"
        exit 0
    fi
    echo "Invalid saved Slurm job ID: ${job_id_file}" >&2
    exit 68
fi

job_id="$(sbatch \
    --parsable \
    --job-name="${job_name}" \
    --comment="${request_id}" \
    --output="${log_dir}/${package_name}_%j.out" \
    --error="${log_dir}/${package_name}_%j.err" \
    --export="ALL,GENCORE_AUTOMATION_ROOT=${automation_root},GENCORE_REQUEST_FILE=${request_file}" \
    "${automation_root}/build_conda_module.sbatch")"

# sbatch may return jobid or jobid;cluster. Return only the numeric job ID.
job_id="${job_id%%;*}"
if [[ ! "${job_id}" =~ ^[0-9]+$ ]]; then
    echo "Unexpected sbatch response" >&2
    exit 67
fi

temporary_job_id_file="${job_id_file}.tmp.$$"
printf '%s\n' "${job_id}" >"${temporary_job_id_file}"
chmod 640 "${temporary_job_id_file}"
mv -f "${temporary_job_id_file}" "${job_id_file}"

printf '%s\n' "${job_id}"
