#!/usr/bin/env python3
import os
import subprocess
import time
import argparse
from astropy.wcs import WCS
import matplotlib.pyplot as plt
import numpy as np

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # 
# The sbatch file below is needed along this python file
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # 
'''
#!/bin/bash
#SBATCH --array=1-31%31
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=20GB
#SBATCH --job-name=cube_split
#SBATCH --output=logs/cube_split-%A-%a.out
#SBATCH --error=logs/cube_split-%A-%a.err
#SBATCH --partition=Main
#SBATCH --time=01:00:00
#SBATCH --account=b09-mightee-ag

cat /etc/hostname

singularity exec /users/lennart/container/meerkat-pol.simg python3 /users/lennart/software/beta/mightee_pol/cube_split.py --slurmArrayTaskId ${SLURM_ARRAY_TASK_ID}
'''
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # 

def run_rmsy_job(args):
    from casatools import image as IA
    from casatasks import imsubimage, exportfits
    n_pieces = int(args.parallel)

    y_size = False
    for inputFits in [args.inputFitsStokesQ, args.inputFitsStokesU]:
        print(inputFits)
        print("asd")
        ia = IA()
        ia.open(f"{inputFits}")
        shape = ia.shape()
        axes = ia.coordsys().names()
        print(shape)
        print(axes.index("Right Ascension"))
        ia.done()
        #xmax = shape[axes.index("Right Ascension")] - 1

        # if condition: otherwise y_size is recalculated every time. I don't know why
        #if not y_size:
            #y_size = ((shape[1]) / n_pieces)
        # [CHANGE 2026-06-10]: Auto-calculate y_size based on image height and parallel tasks
        # Reason: Hardcoded y_size=25 doesn't scale with different image dimensions.
        y_size = int(shape[1] / int(args.parallel))  # in [px]
        xmax = shape[0] - 1
        y1 = (shape[1]) - int(args.slurmArrayTaskId) * y_size
        y2 = (shape[1] - 1) - (int(args.slurmArrayTaskId) - 1) * y_size

        # [CHANGE 2026-06-16]: Last chunk absorbs the y residual to prevent spatial offset
        # Reason: When shape[1] is not divisible by parallel, int(shape[1]/parallel)
        # truncates and the bottom (shape[1] - parallel*y_size) pixels were left
        # unchunked. The merge then placed chunks starting at output y=0, which
        # SHIFTED the entire merged cube upward in pixel space by that residual.
        if int(args.slurmArrayTaskId) == int(args.parallel):
            y1 = 0

        # [CHANGE 2026-06-16]: Geometry sanity check - log every chunk's y range
        # and yell if it's empty, off the image, or fails to cover the residual.
        residual = shape[1] - int(args.parallel) * y_size
        chunk_height = y2 - y1 + 1
        expected_height = y_size if int(args.slurmArrayTaskId) != int(args.parallel) else (y_size + residual)
        print(f"[GEOM] task {args.slurmArrayTaskId}/{args.parallel}: "
              f"y=[{y1},{y2}] height={chunk_height} (expected {expected_height}) "
              f"residual={residual}")
        if y1 < 0 or y2 >= shape[1] or y1 > y2:
            raise ValueError(
                f"[GEOM] Chunk range out of bounds: task {args.slurmArrayTaskId} "
                f"y=[{y1},{y2}], image height={shape[1]}"
            )
        if chunk_height != expected_height:
            raise ValueError(
                f"[GEOM] Chunk size mismatch: task {args.slurmArrayTaskId} "
                f"got {chunk_height} rows, expected {expected_height}"
            )
        reg = f"box[[0pix,{y2}pix],[{xmax}pix,{y1}pix]]"
        outfile = f"part_{args.slurmArrayTaskId}_{inputFits}"
        print(y1, y2)
        print("!!!!!!!!!!")
        imsubimage(imagename=f"{inputFits}", outfile=f"processing/{outfile}.im", region=reg)
        exportfits(imagename=f"processing/{outfile}.im", fitsimage=f"processing/{outfile}".replace(".im",""))

#        try:
#            command = f'singularity exec /idia/software/containers/rm-csromer.sif /users/lennart/venv/bin/rmsynth3d {c.inputFitsStokesQ} {c.inputFitsStokesU} {c.freqList} -o part_{c.slurmArrayTaskId}_ && '
#            command += f'/users/lennart/venv/bin/rmclean3d -c {c.rmsyCleanThrethold} -n {c.rmsyCleanIterations} part_{c.slurmArrayTaskId}_FDF_tot_dirty.fits part_{c.slurmArrayTaskId}_RMSF_tot.fits'
#            sbatchResult = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, shell=True)
#            sbatchResultStd = sbatchResult.stdout.replace("\n", " ")
#            print(sbatchResultStd)
#            # parse the slurm job ID from sbatchResult
#            #slurmIDList = [ int(num) for num in sbatchResultStd.split() if num.isdigit() ]
#            if sbatchResult.stderr:
#                sbatchResultStderrList = sbatchResult.stderr.split("\n")
#                for sbatchResultStderr in sbatchResultStderrList:
#                    print(sbatchResultStderr)
#        except:
#            pass


def write_sbatch_file(args):
    filename_sbatch = __file__.replace(".py", ".sbatch")
    print(f"Writing sbatch file: {filename_sbatch}")
    # [CHANGE 2026-06-10]: Account parameter added to sbatch file generation
    # Reason: Account was hardcoded, requiring code edits for different projects.
    # Now accepts --account parameter, defaults to b09-mightee-ag if not provided.
    # [CHANGE 2026-06-11]: Containerised all binaries (no venv calls)
    # Reason: Match processMeerKAT design — every binary is invoked via
    # 'singularity exec <container>'. No reliance on host filesystem venvs.
    # [CHANGE 2026-06-11]: Resume safety — skip stages whose output already exists
    # Reason: Long-running jobs sometimes fail or hit SLURM time limits. Re-submitting
    # the same array job now picks up where it left off instead of redoing everything.
    # Borrowed from the user's own rmtools_pipeline bash script (resume-safe design).
    sbatch_content = f'''#!/bin/bash
#SBATCH --array=1-{args.parallel}%{args.parallel}
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=10GB
#SBATCH --job-name=rmsy
#SBATCH --output=logs/rmsy-%A-%a.out
#SBATCH --error=logs/rmsy-%A-%a.err
#SBATCH --partition=Main
#SBATCH --time=10:00:00
#SBATCH --account={args.account}

cat /etc/hostname

TASKID=${{SLURM_ARRAY_TASK_ID}}
Q_CHUNK="processing/part_${{TASKID}}_{args.inputFitsStokesQ}"
U_CHUNK="processing/part_${{TASKID}}_{args.inputFitsStokesU}"
FDF_DIRTY="processing/part_${{TASKID}}_FDF_tot_dirty.fits"
FDF_CLEAN="processing/part_${{TASKID}}_FDF_clean_tot.fits"

# [CHANGE 2026-06-11]: Per-stage timing log
# Reason: User wants to track average time per chunk for each stage so they can
# tune chunks/iterations/threshold over time. Appends a CSV row per stage:
#   jobid,taskid,stage,duration_sec,status,timestamp
TIMING_LOG="logs/timings.csv"
mkdir -p logs
if [ ! -f "$TIMING_LOG" ]; then
    echo "jobid,taskid,stage,duration_sec,status,timestamp" > "$TIMING_LOG"
fi
log_stage() {{
    local stage="$1"
    local duration="$2"
    local status="$3"
    echo "${{SLURM_ARRAY_JOB_ID}},${{TASKID}},${{stage}},${{duration}},${{status}},$(date -Iseconds)" >> "$TIMING_LOG"
}}

# Stage 1: Chunk input cubes (skip if already done)
if [ -f "$Q_CHUNK" ] && [ -f "$U_CHUNK" ]; then
    echo "[Stage 1] SKIP — chunks already exist for task $TASKID"
    log_stage "chunking" "0" "skipped"
else
    echo "[Stage 1] Chunking inputs for task $TASKID"
    t0=$SECONDS
    singularity exec {args.casaContainer} python3 {__file__} --parallel {args.parallel} --slurmArrayTaskId ${{TASKID}} --inputFitsStokesQ {args.inputFitsStokesQ} --inputFitsStokesU {args.inputFitsStokesU} --freqList {args.freqList} --casaContainer {args.casaContainer} --rmContainer {args.rmContainer}
    rc=$?
    log_stage "chunking" "$((SECONDS - t0))" "$([ $rc -eq 0 ] && echo OK || echo FAIL)"
fi

# Stage 2: RM synthesis (skip if FDF_tot_dirty exists)
if [ -f "$FDF_DIRTY" ]; then
    echo "[Stage 2] SKIP — RM synthesis already done for task $TASKID"
    log_stage "rmsynth" "0" "skipped"
else
    echo "[Stage 2] Running rmsynth3d for task $TASKID"
    t0=$SECONDS
    singularity exec {args.rmContainer} rmsynth3d -l 1000 "$Q_CHUNK" "$U_CHUNK" {args.freqList} -o part_${{TASKID}}_
    rc=$?
    log_stage "rmsynth" "$((SECONDS - t0))" "$([ $rc -eq 0 ] && echo OK || echo FAIL)"
fi

# Stage 3: RM clean (skip if FDF_clean_tot exists)
if [ -f "$FDF_CLEAN" ]; then
    echo "[Stage 3] SKIP — RM clean already done for task $TASKID"
    log_stage "rmclean" "0" "skipped"
else
    echo "[Stage 3] Running rmclean3d for task $TASKID"
    t0=$SECONDS
    singularity exec {args.rmContainer} rmclean3d -c {args.rmsyCleanThrethold} -n {args.rmsyCleanIterations} -g {args.rmsyCleanGain} {('-w ' + str(args.rmsyCleanWindow)) if args.rmsyCleanWindow > 0 else ''} "$FDF_DIRTY" processing/part_${{TASKID}}_RMSF_tot.fits -o part_${{TASKID}}_
    rc=$?
    log_stage "rmclean" "$((SECONDS - t0))" "$([ $rc -eq 0 ] && echo OK || echo FAIL)"
fi

# Cleanup intermediate chunks once RM clean completed
if [ -f "$FDF_CLEAN" ]; then
    rm -f "$Q_CHUNK" "$U_CHUNK"
    rm -rf "${{Q_CHUNK}}.im" "${{U_CHUNK}}.im"
fi
    '''
    with open(filename_sbatch, "w") as f:
        f.write(sbatch_content)



def setup():
    diretories = ["logs", "processing"]

    for directory in diretories:
        if not os.path.exists(directory):
            os.makedirs(directory)

def submit_rmsy_slurm_job(args):
    run_script = __file__.replace('.py', '.sbatch')
    command = f"SLURMID=$(sbatch {run_script} | cut -d ' ' -f4) && echo SLURMID: "
#    for runScript in conf.input.runScripts[1:]:
#        sbatchScript = runScript.replace(".py", ".sbatch")
#        command += f"$SLURMID;SLURMID=$(sbatch --dependency=afterany:$SLURMID {sbatchScript} | cut -d ' ' -f4) && echo "
    command += "$SLURMID && echo Slurm jobs submitted!"
    print(f"Slurm command: {command}")
    sbatchResult = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, shell=True)
    sbatchResultStd = sbatchResult.stdout.replace("\n", " ")
    print(sbatchResultStd)
    # parse the slurm job ID from sbatchResult
    #slurmIDList = [ int(num) for num in sbatchResultStd.split() if num.isdigit() ]
    if sbatchResult.stderr:
        sbatchResultStderrList = sbatchResult.stderr.split("\n")
        for sbatchResultStderr in sbatchResultStderrList:
            print(sbatchResultStderr)
    return None
# helper functions
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

# [CHANGE 2026-06-10]: Replaced click with argparse for better --help support
# Reason: Proper argument parser provides --help documentation for users.
# Now uses standard argparse with args.tagname syntax instead of custom DotMap.
def main():
    parser = argparse.ArgumentParser(
        description="RM Synthesis pipeline: splits Stokes cubes and performs RM synthesis in parallel",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Create sbatch file with 100 parallel tasks
  ./run_parallel_rmsy.py --parallel 100 --inputFitsStokesQ mycube.stokesQ.fits \\
    --inputFitsStokesU mycube.stokesU.fits --freqList mycube.freqlist.txt \\
    --rmsyCleanThrethold 0.0000010 --rmsyCleanIterations 50 --account b234-llus-ag --createSbatch

  # Submit the SLURM job (after creating sbatch)
  ./run_parallel_rmsy.py --parallel 100 --start

  # Run a single job array task (called by SLURM, not manually)
  ./run_parallel_rmsy.py --parallel 100 --slurmArrayTaskId 1 \\
    --inputFitsStokesQ mycube.stokesQ.fits --inputFitsStokesU mycube.stokesU.fits \\
    --freqList mycube.freqlist.txt
        """
    )

    parser.add_argument('--parallel', type=int, required=True,
                        help='Number of parallel tasks to split the image into')
    parser.add_argument('--inputFitsStokesQ',
                        help='Path to Stokes Q FITS cube')
    parser.add_argument('--inputFitsStokesU',
                        help='Path to Stokes U FITS cube')
    parser.add_argument('--freqList',
                        help='Path to frequency list file (one frequency per line in Hz)')
    parser.add_argument('--slurmArrayTaskId', type=int,
                        help='SLURM array task ID (set by SLURM, used to process one strip)')
    parser.add_argument('--rmsyCleanThrethold', type=float, default=0.0000010,
                        help='RM clean threshold (default: 0.0000010)')
    parser.add_argument('--rmsyCleanWindow', type=float, default=0.0,
                        help='-w WINDOW for rmclean3d. 0 = skip second pass (default).')
    parser.add_argument('--rmsyCleanGain', type=float, default=0.1,
                        help='-g GAIN for rmclean3d (default: 0.1)')
    parser.add_argument('--rmsyCleanIterations', type=int, default=50,
                        help='RM clean iterations (default: 50)')
    parser.add_argument('--account', default='b09-mightee-ag',
                        help='SLURM account for job submission (default: b09-mightee-ag)')
    # [CHANGE 2026-06-11]: Added container path arguments
    # Reason: Pipeline now sources all tools from containers (matches processMeerKAT design).
    # No reliance on host-side venvs like ~/myvenvs/RM-env/.
    parser.add_argument('--casaContainer',
                        default='/idia/software/containers/casa-6.4.4-modular.simg',
                        help='Path to CASA Singularity container (for chunking)')
    parser.add_argument('--rmContainer', required=False,
                        default='/idia/projects/uct-mega-hi/Pro_M_Prac/containers/rm-env.sif',
                        help='Path to rm-env Singularity container (for rmsynth3d/rmclean3d)')
    parser.add_argument('--createSbatch', action='store_true',
                        help='Write sbatch file with current parameters')
    parser.add_argument('--start', action='store_true',
                        help='Submit the sbatch job to SLURM')

    args = parser.parse_args()
    setup()

    print(f"Script arguments: {args}")

    if args.createSbatch:
        write_sbatch_file(args)

    if args.start:
        submit_rmsy_slurm_job(args)

    if args.slurmArrayTaskId:
        print("Running RM synthesis job for task ID:", args.slurmArrayTaskId)
        run_rmsy_job(args)

if __name__ == "__main__":
    main()
