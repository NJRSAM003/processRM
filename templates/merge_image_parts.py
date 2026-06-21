#!/usr/bin/env python3

import itertools
#import logging
#from logging import info, error
import os
import time
import csv
import datetime
from glob import glob
import re
import sys
import argparse
import subprocess

import numpy as np
from astropy.io import fits


def make_empty_image(inputName, initial_fits_header, mode="normal"):
    """
    Generate an empty FITS file sized to hold the merged output of one rmsynth3d
    product (FDF_*_tot, RMSF_*, FDF_maxPI, RMSF_FWHM, ...).

    rmsynth3d writes some products as 4D cubes (1, NPHI, NY, NX) and others
    as bare 2D images (NY, NX). We inspect the first chunk to decide which
    shape to allocate, and write a header whose NAXIS keyword count agrees
    with the actual data rank -- otherwise astropy's verify refuses to flush
    the file with errors like "NAXIS3 out of range when NAXIS == 2".
    """
    cubeNameInput = inputName

    hduCubeInput = fits.open("processing/part_1_" + cubeNameInput, memmap=True, mode="update")
    sample_shape = np.squeeze(hduCubeInput[0].data).shape

    # [CHANGE 2026-06-21]: detect 2D vs 3D+ chunks explicitly. Previously the
    # try/except always treated 2D inputs as 4D (zdim=wdim=1) with NAXIS=4 in
    # the header, which crashed astropy's verify on close.
    is_2d = len(sample_shape) < 3
    if is_2d:
        zdim = wdim = None
    else:
        zdim = sample_shape[-3]
        wdim = 1

    xdim, ydim = initial_fits_header['NAXIS1'], initial_fits_header['NAXIS2']
    dims = (xdim, ydim) if is_2d else (xdim, ydim, zdim, wdim)

    dummy_dims = tuple(1 for _ in dims)
    dummy_data = np.zeros(dummy_dims, dtype=np.float32)
    hdu = fits.PrimaryHDU(data=dummy_data)

    header = hduCubeInput[0].header
    # [CHANGE 2026-06-22]: rewrite NAXIS keys IN PLACE rather than del + re-add.
    # FITS requires NAXIS1..NAXISn to appear immediately after NAXIS in the
    # header; deleting them and re-assigning shoves them to the end and
    # astropy's verify rejects the file with "'NAXIS1' card at the wrong place".
    new_n = len(dims)
    old_n = int(header.get('NAXIS', 0))
    # 1) strip axes we don't want any more (e.g. going from 4D input to 2D out)
    for j in range(new_n + 1, max(old_n, new_n) + 1):
        key = f"NAXIS{j}"
        if key in header:
            del header[key]
    # 2) update NAXIS itself, then each NAXISn -- header.set's after= argument
    #    is ignored for already-present keys (so it updates in place), and used
    #    to insert in the correct position when the key is new.
    header['NAXIS'] = new_n
    for i, dim in enumerate(dims, 1):
        anchor = 'NAXIS' if i == 1 else f'NAXIS{i-1}'
        header.set(f'NAXIS{i}', dim, after=anchor)

    cubeNameOutput = inputName

    header.tofile(cubeNameOutput, overwrite=True)

    # create full-sized zero image

    header_size = len(
        header.tostring()
    )  # Probably 2880. We don't pad the header any more; it's just the bare minimum
    # [CHANGE 2026-06-11]: Replaced np.product with np.prod (NumPy compatibility)
    # Reason: np.product() was deprecated and removed in NumPy 2.0+. np.prod() is the replacement.
    data_size = np.prod(dims) * np.dtype(np.float32).itemsize
    # This is not documented in the example, but appears to be Astropy's default behaviour
    # Pad the total file size to a multiple of the header block size
    block_size = 2880
    data_size = block_size * (((data_size -1) // block_size) + 1)

    with open(cubeNameOutput, "rb+") as f:
        f.seek(header_size + data_size - 1)
        f.write(b"\0")
    print("Creating empty cube:", cubeNameOutput)


def update_fits_header_of_cube(filepathCube, headerDict):
    '''
    '''
    print(f"Updating header for file: File: {filepathCube}, Update: {headerDict}")
    with fits.open(filepathCube, memmap=True, ignore_missing_end=True, mode="update") as hud:
        header = hud[0].header
        for key, value in headerDict.items():
            header[key] = value


def fix_invalid_stokes_axis(filepathCube):
    """
    [CHANGE 2026-06-11]: Fix CRVAL4 = 0 on FDF_tot / RMSF_tot / FDF_maxPI / FDF_peakRM
    Reason: rmsynth3d outputs these with CRVAL4=0 (invalid Stokes value).
    CARTA refuses to open files with invalid Stokes ref values
    ("cannot determine coordinate axes from incomplete header").
    Set CRVAL4 = 1 (treat as Stokes I, an intensity-like quantity)
    so CARTA accepts the file. Valid Stokes: 1=I 2=Q 3=U 4=V.
    """
    tot_like_suffixes = (
        'FDF_tot_dirty.fits', 'FDF_clean_tot.fits', 'FDF_CC_tot.fits',
        'RMSF_tot.fits', 'FDF_maxPI.fits', 'FDF_peakRM.fits',
    )
    if not filepathCube.endswith(tot_like_suffixes):
        return
    with fits.open(filepathCube, mode='update') as hud:
        header = hud[0].header
        # CRVAL4 is meaningless on 2D outputs (FDF_maxPI / FDF_peakRM /
        # RMSF_FWHM) -- writing it would trigger an astropy verify error
        # on close because NAXIS=2 has no axis 4.
        if int(header.get('NAXIS', 0)) < 4:
            return
        crval4 = header.get('CRVAL4', None)
        if crval4 == 0 or crval4 is None:
            header['CRVAL4'] = 1
            print(f"  -> Fixed CRVAL4 (was {crval4}, now 1) on {filepathCube}")

def fill_cube_with_images(outputName, listing_all_parts, initial_fits_header):
    """
    Fills the empty data cube with fits data.


    """
    cubeNameOutput = outputName

    listing_all_parts_for_one_image = [x for x in listing_all_parts if x.endswith(outputName)]
    for ii in listing_all_parts_for_one_image:
        print(ii)
    y_height = 0
    hudCubeOutput = fits.open(cubeNameOutput, memmap=True, ignore_missing_end=True, mode="update")
    dataCubeOutput = hudCubeOutput[0].data
    for sub_image in reversed(listing_all_parts_for_one_image):
        print("Processing:", sub_image)
        hud_sub_image_input = fits.open(sub_image, memmap=True, ignore_missing_end=True, mode="update")
        data_sub_image = hud_sub_image_input[0].data
        y, x = data_sub_image.shape[-2:]

        print(y+y_height, y)
        # [CHANGE 2026-06-21]: handle all rmsynth3d output ranks uniformly.
        # FDF_*_tot / RMSF_tot come out 4D (1, NPHI, NY, NX); FDF_maxPI,
        # FDF_peakRM, RMSF_FWHM come out 2D (NY, NX). The old try/except
        # ladder only had 4D and 3D paths, so 2D outputs crashed with
        # 'too many indices'. An ellipsis slice on the (always 4D) output
        # broadcasts cleanly against 2D / 3D / 4D sources alike.
        dataCubeOutput[..., y_height:y+y_height, :] = data_sub_image

        y_height += y
        hud_sub_image_input.close()

    # [CHANGE 2026-06-16]: Coverage sanity check
    # Refuse to declare the merge "done" if the assembled chunks don't fill
    # the entire NAXIS2 of the output cube. Anything less than NAXIS2 means
    # we left an unchunked residual at the bottom and the WCS is misaligned.
    expected_y = initial_fits_header.get('NAXIS2', None)
    if expected_y is not None and y_height != expected_y:
        raise ValueError(
            f"[MERGE] Coverage mismatch for {cubeNameOutput}: assembled "
            f"{y_height} rows but the output cube expects {expected_y}. "
            f"Difference of {expected_y - y_height} rows would offset the WCS. "
            "Re-check the chunker (run_parallel_rmsy.py); the last chunk should "
            "absorb any (shape[1] %% parallel) residual."
        )

    update_fits_header_of_cube(cubeNameOutput, initial_fits_header)
    hudCubeOutput.close()

    print(f"Cube filled OK: {cubeNameOutput} (rows assembled: {y_height})")


def create_all_cubes(inputcube, slurmArrayTaskId):
    # get CRPIx value and x, y length
    cubeNameInput = inputcube
    hduCubeInput = fits.open(cubeNameInput, memmap=True, mode="update")
    header = hduCubeInput[0].header
    # [CHANGE 2026-06-11]: Expanded initial_fits_header to include all WCS keywords
    # Reason: FDF_tot cubes were missing CRVAL, CDELT, CTYPE, CUNIT keywords, causing
    # CARTA to fail with "cannot determine coordinate axes from incomplete header" error.
    # Now copying full WCS header to ensure proper coordinate system in output cubes.
    initial_fits_header = {
            "NAXIS1": header["NAXIS1"],
            "NAXIS2": header["NAXIS2"],
            "CRPIX1": header["CRPIX1"],
            "CRPIX2": header["CRPIX2"],
            "CRVAL1": header.get("CRVAL1", 0.0),
            "CRVAL2": header.get("CRVAL2", 0.0),
            "CDELT1": header.get("CDELT1", 1.0),
            "CDELT2": header.get("CDELT2", 1.0),
            "CTYPE1": header.get("CTYPE1", "RA---TAN"),
            "CTYPE2": header.get("CTYPE2", "DEC--TAN"),
            "CUNIT1": header.get("CUNIT1", "deg"),
            "CUNIT2": header.get("CUNIT2", "deg"),
            }

    def get_part_number(x):
        return int(x.split("part_")[1].split("_")[0])
    listing_all_parts = sorted(glob("processing/*fits"), key = get_part_number)

    # [CHANGE 2026-06-22]: only merge rmsynth3d / rmclean3d output products.
    # processing/ also contains the input Q/U cube chunks and noise_map slices
    # we keep around for fullSummary's chunking bar; if their basenames leak
    # into listing_basenames the merge would overwrite the cropped Q/U/noise
    # files in the region root with empty-header placeholders and the array
    # tasks would race each other to corrupt them. Restrict to the names
    # rmsynth3d / rmclean3d produce.
    def _is_rmsynth_output(basename):
        n = basename.lower()
        return n.startswith('fdf_') or n.startswith('rmsf_') or n.startswith('clean_')

    all_basenames = sorted({
        x.split("part_", 1)[1].split("_", 1)[1]
        for x in listing_all_parts
    })
    rmsynth_basenames = [bn for bn in all_basenames if _is_rmsynth_output(bn)]
    if slurmArrayTaskId:
        listing_basenames = rmsynth_basenames[int(slurmArrayTaskId) - 1:int(slurmArrayTaskId)]
    else:
        listing_basenames = rmsynth_basenames
    print(listing_basenames)

    #for inputName in listing_all_parts[:len(listing_basenames)]:
    for inputName in listing_basenames:
        make_empty_image(inputName, initial_fits_header)

    for inputName in listing_basenames:
        fill_cube_with_images(inputName, listing_all_parts, initial_fits_header)

    # Apply Stokes-axis fix BEFORE fits2idia conversion
    for inputName in listing_basenames:
        fix_invalid_stokes_axis(inputName)

    # [CHANGE 2026-06-11]: Containerised fits2idia call
    # Reason: fits2idia must run inside the rm-env container (or a dedicated IDIA container).
    # Currently using CASA container as fallback if fits2idia not present in rm-env.
    rm_container = os.environ.get("PROCESSRM_RM_CONTAINER", "")
    for inputName in listing_basenames:
        if rm_container:
            command = f"singularity --quiet exec {rm_container} fits2idia -s -p {inputName}"
        else:
            command = f"fits2idia -s -p {inputName}"
        print(f"Command: {command}")
        sbatchResult = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, shell=True)
        sbatchResultStd = sbatchResult.stdout.replace("\n", " ")
        print(sbatchResultStd)
        # parse the slurm job ID from sbatchResult
        #slurmIDList = [ int(num) for num in sbatchResultStd.split() if num.isdigit() ]
        if sbatchResult.stderr:
            sbatchResultStderrList = sbatchResult.stderr.split("\n")
            for sbatchResultStderr in sbatchResultStderrList:
                print(sbatchResultStderr)


def write_sbatch_file(inputcube, account='b09-mightee-ag', rm_container='', casa_container='', job_name_suffix=''):
    def get_part_number(x):
        return int(x.split("part_")[1].split("_")[0])
    listing_all_parts = sorted(glob("processing/*fits"), key = get_part_number)
    # Mirror create_all_cubes: only count rmsynth3d / rmclean3d output products
    # so the merge array's size matches the actual number of merge targets.
    def _is_rmsynth_output(basename):
        n = basename.lower()
        return n.startswith('fdf_') or n.startswith('rmsf_') or n.startswith('clean_')
    rmsynth_basenames = {
        bn for bn in (x.split("part_", 1)[1].split("_", 1)[1] for x in listing_all_parts)
        if _is_rmsynth_output(bn)
    }
    length_listing_basenames = len(rmsynth_basenames)

    filename_sbatch = __file__.replace(".py", ".sbatch")
    print(f"Writing sbatch file: {filename_sbatch}")
    # [CHANGE 2026-06-11]: Containerised merge step
    # Reason: Match processMeerKAT design — script runs inside rm-env container
    # via 'singularity exec'. Environment variable PROCESSRM_RM_CONTAINER is passed
    # so fits2idia call inside the script also uses the container.
    runner = f"singularity --quiet exec {rm_container}" if rm_container else ""
    env_export = f"export PROCESSRM_RM_CONTAINER={rm_container};" if rm_container else ""
    sbatch_content = f'''#!/bin/bash
#SBATCH --array=1-{length_listing_basenames}
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=100GB
#SBATCH --job-name=merge{job_name_suffix}
#SBATCH --output=logs/merge{job_name_suffix}-%A-%a.out
#SBATCH --error=logs/merge{job_name_suffix}-%A-%a.err
#SBATCH --partition=Main
#SBATCH --time=20:00:00
#SBATCH --account={account}

cat /etc/hostname

# [CHANGE 2026-06-11]: Per-stage timing log for merging
TIMING_LOG="logs/timings.csv"
mkdir -p logs
if [ ! -f "$TIMING_LOG" ]; then
    echo "jobid,taskid,stage,duration_sec,status,timestamp" > "$TIMING_LOG"
fi

{env_export}
t0=$SECONDS
{runner} python3 ./merge_image_parts.py --inputcube {inputcube} --slurmArrayTaskId ${{SLURM_ARRAY_TASK_ID}} --account {account} --rmContainer {rm_container} --casaContainer {casa_container}
rc=$?
echo "${{SLURM_ARRAY_JOB_ID}},${{SLURM_ARRAY_TASK_ID}},merge,$((SECONDS - t0)),$([ $rc -eq 0 ] && echo OK || echo FAIL),$(date -Iseconds)" >> "$TIMING_LOG"
    '''
    with open(filename_sbatch, "w") as f:
        f.write(sbatch_content)

def submit_slurm_job():
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

# [CHANGE 2026-06-10]: Replaced click with argparse for better --help support
# Reason: Proper argument parser provides --help documentation for users.
# Now uses standard argparse with args.attribute syntax instead of click decorators.
def main():
    parser = argparse.ArgumentParser(
        description="Merge RM-synthesized image parts back into full cubes",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Create sbatch file to merge all parts (run this first)
  ./merge_image_parts.py --inputcube mycube_IQUV.fits

  # Run as individual array task (called by SLURM)
  ./merge_image_parts.py --inputcube mycube_IQUV.fits --slurmArrayTaskId 1
        """
    )

    parser.add_argument('--inputcube', required=True,
                        help='Path to input cube (used for header information)')
    parser.add_argument('--slurmArrayTaskId', type=int,
                        help='SLURM array task ID (set by SLURM, creates one output file per task)')
    # [CHANGE 2026-06-11]: Added --account parameter to merge_image_parts.py
    # Reason: Account was hardcoded in sbatch file, now matches run_parallel_rmsy.py interface.
    # Allows users to specify different project accounts without editing code.
    parser.add_argument('--account', default='b09-mightee-ag',
                        help='SLURM account for job submission (default: b09-mightee-ag)')
    # [CHANGE 2026-06-11]: Containerised — accept container paths
    parser.add_argument('--rmContainer', default='',
                        help='Path to rm-env Singularity container (for fits2idia)')
    parser.add_argument('--casaContainer',
                        default='/idia/software/containers/casa-6.4.4-modular.simg',
                        help='Path to CASA Singularity container')
    parser.add_argument('--jobNameSuffix', default='',
                        help='Optional suffix appended to the SLURM job name and log filenames '
                             '(used by the per-region orchestrator, e.g. "_r1", "_r2").')

    args = parser.parse_args()

    # Export container path for child fits2idia call
    if args.rmContainer:
        os.environ["PROCESSRM_RM_CONTAINER"] = args.rmContainer

    if not args.slurmArrayTaskId:
        time.sleep(1)
        write_sbatch_file(args.inputcube, account=args.account,
                          rm_container=args.rmContainer,
                          casa_container=args.casaContainer,
                          job_name_suffix=args.jobNameSuffix)
        submit_slurm_job()
    if args.slurmArrayTaskId:
        create_all_cubes(args.inputcube, slurmArrayTaskId=args.slurmArrayTaskId)


if __name__ == "__main__":
    main()
