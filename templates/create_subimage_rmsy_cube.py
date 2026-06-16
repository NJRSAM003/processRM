#!/usr/bin/env python3

import itertools
#import logging
#from logging import info, error
import os
import csv
import datetime
from glob import glob
import re
import sys
import argparse
import subprocess

import numpy as np
from astropy.io import fits

def write_cube_frequency_list(filepathCube, freqPower=1e-9):
    '''
    '''
    print(f"Getting channel frequency list for cube: {filepathCube}") 
    freq_list = []
    with fits.open(filepathCube, memmap=True, mode="update") as hud:
        #dataCube = hud[0].data
        headerCube = hud[0].header
        maxIdx = hud[0].data.shape[1]
    refChanIdx = int(headerCube['CRPIX3']) -1
    for ii in range(0, maxIdx):
        freq = float(headerCube['CRVAL3']) + float(headerCube['CDELT3']) * (-refChanIdx + ii)
        freq_list.append(str(freq))
    with open(filepathCube.replace(".fits", ".freqlist.txt"), "w") as f:
        f.write("\n".join(freq_list))
    print(f"Writing frequency list to file: {filepathCube}") 
    #return freq_list

def make_empty_image(inputName, crop, pointing, mode="normal"):
    """
    Generate an empty dummy fits data cube.

    The data cube dimensions are derived from the cube images.

    """
    cubeNameInput = inputName
        
    hduCubeInput = fits.open(cubeNameInput, memmap=True, mode="update")
    zdim, ydim, xdim = np.squeeze(hduCubeInput[0].data).shape[-3:]

    #zdim = 1
    #zdim = 197
    zdim = 5

    wdim = 1

    #xdim, ydim = get_cropped_size_in_px(conf)
    if crop:
        xdim, ydim = list(crop)

    dims = tuple([xdim, ydim, zdim, wdim])

    # create header

    dummy_dims = tuple(1 for d in dims)
    #dummy_data = np.ones(dummy_dims, dtype=np.float64) * np.nan
    #dummy_data = dummy_data.fill(np.nan)
    dummy_data = np.zeros(dummy_dims, dtype=np.float32)
    hdu = fits.PrimaryHDU(data=dummy_data)

    header = hduCubeInput[0].header
    for i, dim in enumerate(dims, 1):
        header["NAXIS%d" % i] = dim

    #header["NAXIS"] = 3
    #del header["NAXIS4"]
    #del header["PC1_4"]
    #del header["PC2_4"]
    #del header["PC3_4"]
    #del header["PC4_1"]
    #del header["PC4_2"]
    #del header["PC4_3"]
    #del header["PC4_4"]
    #del header["CTYPE4"]
    #del header["CRVAL4"]
    #del header["CDELT4"]
    #del header["CRPIX4"]
    #del header["CUNIT4"]

    if mode == "stokesQ":
        cubeNameOutput = inputName.replace(".fits", ".stokesQ.fits")
    elif mode == "stokesU":
        cubeNameOutput = inputName.replace(".fits", ".stokesU.fits")
    elif mode == "stokesI":
        cubeNameOutput = inputName.replace(".fits", ".RMSY-sub.fits")

    header.tofile(cubeNameOutput, overwrite=True)

    # create full-sized zero image

    header_size = len(
        header.tostring()
    )  # Probably 2880. We don't pad the header any more; it's just the bare minimum
    data_size = np.prod(dims) * np.dtype(np.float32).itemsize
    # This is not documented in the example, but appears to be Astropy's default behaviour
    # Pad the total file size to a multiple of the header block size
    block_size = 2880
    data_size = block_size * (((data_size -1) // block_size) + 1)

    with open(cubeNameOutput, "rb+") as f:
        f.seek(header_size + data_size - 1)
        f.write(b"\0")
    print("Creating empty cube:", cubeNameOutput)


def get_cropped_numpy_plane(crop, pointing, plane):
    if crop and pointing:
        number_of_channels, plane_height, plane_width = plane.shape
        print(plane_width, plane_height)
        width, height = crop  #get_cropped_size_in_px(conf)
        print(width, height)

        if plane_width < width or plane_height < height:
            #info(f"Input dimensions {plane_width}px,{plane_height}px are lower than target '--crop {conf.input.crop}'")
            #info(f"Falling back to: {plane_width}px,{plane_height}px")
            width = plane_width
            height = plane_height

        left = int(pointing[0] - width/2)
        top = int(pointing[1] - height/2)
        right = int(pointing[0] + width/2)
        bottom = int(pointing[1] + height/2)
        plane = plane[:, top:bottom, left:right]
        print(plane.shape)
    return plane


def update_fits_header_of_cube(filepathCube, headerDict):
    '''
    '''
    print(f"Updating header for file: File: {filepathCube}, Update: {headerDict}")
    with fits.open(filepathCube, memmap=True, ignore_missing_end=True, mode="update") as hud:
        header = hud[0].header
        for key, value in headerDict.items():
            header[key] = value

def fill_cube_with_images(inputName, crop, pointing, mode="normal"):
    """
    Fills the empty data cube with fits data.


    """
    cubeNameInput = inputName
    if mode == "stokesQ":
        cubeNameOutput = inputName.replace(".fits", ".stokesQ.fits")
    elif mode == "stokesU":
        cubeNameOutput = inputName.replace(".fits", ".stokesU.fits")
    elif mode == "stokesI":
        cubeNameOutput = inputName.replace(".fits", ".RMSY-sub.fits")

    hudCubeInput = fits.open(cubeNameInput, memmap=True, ignore_missing_end=True, mode="update")
    dataCubeInput = hudCubeInput[0].data

    hudCubeOutput = fits.open(cubeNameOutput, memmap=True, ignore_missing_end=True, mode="update")
    dataCubeOutput = hudCubeOutput[0].data

#    highestChannel = int(dataCubeInput.shape[1])
    if mode == "stokesQ":
        #dataCubeOutput[0, :, :, :] = np.nan_to_num(dataCubeInput[1, :, :, :])
        dataCubeOutput[0, :, :, :] = get_cropped_numpy_plane(crop, pointing, dataCubeInput[1, :5, :, :])
    elif mode == "stokesU":
        dataCubeOutput[0, :, :, :] = get_cropped_numpy_plane(crop, pointing, dataCubeInput[2, :5, :, :])
    elif mode == "stokesI":
        dataCubeOutput[0, :, :, :] = get_cropped_numpy_plane(crop, pointing, dataCubeInput[0, :5, :, :])
#    dataCubeOutput[0, :, :] = dataCubeInput[1, 0, :, :]
#    dataCubeOutput[0, :, :] = np.sqrt(dataCubeInput[1, 0, :, :]**2 + dataCubeInput[2, 0, :, :]**2)
#    dataMedian = np.median(dataCubeOutput[:, :, :])
#    stokesV_median = np.median(dataCubeInput[2, 0, :, :])
#    dataCubeOutput[0, :, :] = dataCubeInput[1, 0, :, :]

    wdim, zdim, xdim, ydim = dataCubeInput.shape
    zdim = 5
    if crop and pointing:
        addFitsHeaderDict = {
                #"CRPIX3": 1, #lowestChanNo,
                "CRPIX1": int(xdim/2 - pointing[0] + crop[0]/2),
                "CRPIX2": int(ydim/2 - pointing[1] + crop[0]/2)
                #"OBJECT": str(conf.data.field),
                #"NAXIS3": highestChannel,
                #"CTYPE3": ("FREQ", ""),
                #"COMMENT": "Created by IDIA Pipeline"
                }
        update_fits_header_of_cube(cubeNameOutput, addFitsHeaderDict)

    hudCubeInput.close()
    hudCubeOutput.close()
    print("Cube filled:", cubeNameOutput)

def run_command(command):
    '''
    TODO: return error code?
    '''
    #info(SEPERATOR_SOFT)
    print(f"Running command outside of python environment. Error messages may not be reliable.")
    print(f"Command: {command}")
    cmdResult = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, shell=True)
    cmdResultStdList = cmdResult.stdout.split("\n")
    for cmdResultStd in cmdResultStdList:
        print(cmdResultStd)
    if cmdResult.stderr:
        cmdResultStderrList = cmdResult.stderr.split("\n")
        for cmdResultStderr in cmdResultStderrList:
            print(cmdResultStderr)
    #info(SEPERATOR_SOFT)


# [CHANGE 2026-06-10]: Replaced click with argparse for better --help support
# Reason: Proper argument parser provides --help documentation for users.
# Now uses standard argparse with args.attribute syntax instead of click decorators.
def main():
    parser = argparse.ArgumentParser(
        description="Create RM synthesis subset cube: extracts first 5 channels of Stokes I",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Extract Stokes I subset for RM synthesis testing
  ./create_subimage_rmsy_cube.py --inputcube mycube_IQUV.fits

  # Extract with crop and pointing
  ./create_subimage_rmsy_cube.py --inputcube mycube_IQUV.fits --crop "[800, 800]" --pointing "[3072, 3072]"
        """
    )

    parser.add_argument('--inputcube', required=True,
                        help='Path to input full Stokes cube')
    parser.add_argument('--crop',
                        help='Crop region as "[width, height]" (e.g., "[800, 800]")')
    parser.add_argument('--pointing',
                        help='Pointing center as "[x, y]" (e.g., "[3072, 3072]")')

    args = parser.parse_args()

    try:
        crop = eval(args.crop) if args.crop else False
        pointing = eval(args.pointing) if args.pointing else False
    except:
        crop = False
        pointing = False

    filename = args.inputcube
    basename = args.inputcube.replace(".fits", "")

    write_cube_frequency_list(filename)
    make_empty_image(filename, crop, pointing, mode="stokesI")
    fill_cube_with_images(filename, crop, pointing, mode="stokesI")


if __name__ == "__main__":
    main()
