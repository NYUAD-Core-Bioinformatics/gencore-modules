#!/usr/bin/env bash

# Jubail EasyBuild environment. This file is sourced by the Slurm job.
export PATH="/share/apps/NYUAD5/gcc/9.2.0/bin:${PATH}"
export PATH="/share/apps/NYUAD5/miniconda/3-4.11.0/envs/python3.11/bin:${PATH}"
export LD_LIBRARY_PATH="/scratch/gencore/Gencoremodules_v3/easybuild/tcl_local/lib:${LD_LIBRARY_PATH:-}"
export PATH="/scratch/gencore/Gencoremodules_v3/easybuild/env_modules_5-4-0/bin:${PATH}"

source /scratch/gencore/Gencoremodules_v3/easybuild/env_modules_5-4-0/init/bash

export EASYBUILD_PREFIX=/scratch/gencore/.eb/3.0
export EASYBUILD_CONFIGFILES=/scratch/gencore/.eb/3.0/config.cfg

unset MODULEPATH
export MODULEPATH=/scratch/gencore/.eb/3.0/modules/all
module load EasyBuild/4.9.4
