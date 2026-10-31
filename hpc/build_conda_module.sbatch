#!/usr/bin/env bash
#SBATCH --job-name=gencore-conda-build
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=10
#SBATCH --time=24:00:00
#SBATCH --mem=90G
#SBATCH --partition=gencore

set -Eeuo pipefail

: "${GENCORE_REQUEST_FILE:?GENCORE_REQUEST_FILE was not provided by the submit wrapper}"

automation_root="${GENCORE_AUTOMATION_ROOT:-/scratch/gencore/GencoreModule_automate}"

source "${automation_root}/source_vars.sh"

export PYTHONUNBUFFERED=1

exec python -u "${automation_root}/build_conda_module.py" \
    --request "${GENCORE_REQUEST_FILE}"
