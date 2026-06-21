#!/usr/bin/env python3
"""
Cube structure validation for processRM.

Catches problems that would otherwise blow up deep inside CASA / rmsynth3d:
    * 3D cubes (no Stokes axis) - the pipeline cannot proceed without I/Q/U
    * Swapped axes (RA, DEC, STOKES, FREQ) - auto-transposed to the expected
      (RA, DEC, FREQ, STOKES) order with a WARN
    * Frequency-channel count mismatch against the user-supplied freqlist .txt

All functions accept either a path to a FITS file or an already-opened
``astropy.io.fits.Header``. Astropy is imported lazily so this module can be
imported in places that don't have it (e.g. the BUILD-mode preview when the
user runs processRM on the login node before activating the container).
"""

import os


class CubeStructureError(ValueError):
    """Raised when a cube's structure makes it unusable by rmsynth3d."""


# Axis name normalisation: CTYPE keywords can be 'FREQ', 'FREQ-LSR', 'STOKES',
# 'STOK', etc. We strip the suffix so 'FREQ-OBS' counts as 'FREQ'.
def _axis_kind(ctype):
    if not ctype:
        return ''
    head = ctype.split('-')[0].strip().upper()
    if head in ('FREQ', 'VRAD', 'VOPT'):
        return 'FREQ'
    if head in ('STOKES', 'STOK'):
        return 'STOKES'
    if head in ('RA',):
        return 'RA'
    if head in ('DEC',):
        return 'DEC'
    return head


def detect_axes(header):
    """Return a dict mapping axis kind ('RA','DEC','FREQ','STOKES') to the FITS
    axis number (1-based). Missing axes are absent from the dict."""
    naxis = int(header.get('NAXIS', 0))
    axes = {}
    for i in range(1, naxis + 1):
        kind = _axis_kind(header.get(f'CTYPE{i}', ''))
        if kind in ('FREQ', 'STOKES', 'RA', 'DEC'):
            axes[kind] = i
    return axes


def validate_cube_structure(path):
    """Validate a FITS cube and return a dict describing what we found:

        {
          'naxis': int,
          'axes':  {'RA': 1, 'DEC': 2, 'FREQ': 3, 'STOKES': 4},
          'freq_axis': 3,                # FITS-axis number that carries FREQ
          'stokes_axis': 4,              # FITS-axis number that carries STOKES
          'freq_nchans': int,            # length of the FREQ axis
          'needs_transpose': bool,       # True if axes are (RA,DEC,STOKES,FREQ)
        }

    Raises ``CubeStructureError`` for:
      - non-existent file
      - NAXIS != 4 (we require I/Q/U or at least a Stokes axis to slice)
      - axes that we cannot identify as FREQ / STOKES
    """
    if not os.path.exists(path):
        raise CubeStructureError(f"Cube file not found: {path}")
    try:
        from astropy.io import fits
    except ImportError as e:
        raise CubeStructureError(
            f"astropy is required to validate '{path}' but is not importable: {e}. "
            "Run inside the rm-env container or install astropy locally."
        )

    header = fits.getheader(path)
    naxis = int(header.get('NAXIS', 0))

    if naxis < 4:
        raise CubeStructureError(
            f"Cube '{path}' has NAXIS={naxis}. processRM requires a 4D cube "
            "with axes (RA, DEC, FREQ, STOKES). A 3D image has no Stokes axis, "
            "so there is no Q or U to do RM-synthesis on. Re-image with all "
            "Stokes parameters retained."
        )

    axes = detect_axes(header)
    if 'FREQ' not in axes:
        raise CubeStructureError(
            f"Cube '{path}': could not identify the frequency axis from CTYPE "
            f"headers ({[header.get(f'CTYPE{i}', '') for i in range(1, naxis+1)]}). "
            "Expected one CTYPE to be 'FREQ' (or 'FREQ-LSR', 'FREQ-OBS', ...)."
        )
    if 'STOKES' not in axes:
        raise CubeStructureError(
            f"Cube '{path}': could not identify the Stokes axis from CTYPE "
            f"headers ({[header.get(f'CTYPE{i}', '') for i in range(1, naxis+1)]}). "
            "Expected one CTYPE to be 'STOKES'."
        )

    freq_axis = axes['FREQ']
    stokes_axis = axes['STOKES']
    freq_nchans = int(header[f'NAXIS{freq_axis}'])

    # Expected order: (RA=1, DEC=2, FREQ=3, STOKES=4). Anything else with FREQ
    # and STOKES present but swapped (FREQ=4, STOKES=3) is the case the user
    # asked us to auto-transpose.
    needs_transpose = (freq_axis == 4 and stokes_axis == 3)

    return {
        'naxis': naxis,
        'axes': axes,
        'freq_axis': freq_axis,
        'stokes_axis': stokes_axis,
        'freq_nchans': freq_nchans,
        'needs_transpose': needs_transpose,
    }


def has_beam_info(path):
    """Return how beam info is carried in a FITS cube, or None.

    The PyBDSF-driven noise-map step (RM-Tools-sigma's make_noise_map) needs
    per-channel BMAJ/BMIN/BPA. The two acceptable sources, in priority order:

      'header'      -- BMAJ, BMIN, BPA in the primary HDU header
      'casa_beams'  -- a CASA-style BEAMS table HDU (per-channel beam params)

    Returns one of the strings above, or None if neither is present. Use this
    to decide whether the sigma-cleaning path can run; if it returns None, the
    caller should refuse to submit the noise stage and tell the user to either
    switch to an absolute threshold (positive `[rmclean] threshold`) or re-image
    with beam metadata retained.
    """
    try:
        from astropy.io import fits
    except ImportError:
        return None
    if not os.path.exists(path):
        return None
    try:
        with fits.open(path) as hdul:
            hdr = hdul[0].header
            if all(k in hdr for k in ('BMAJ', 'BMIN', 'BPA')):
                return 'header'
            for hdu in hdul[1:]:
                name = (getattr(hdu, 'name', '') or '').upper()
                if name in ('BEAMS', 'CASA_BEAMS'):
                    return 'casa_beams'
    except OSError:
        return None
    return None


def count_freqlist_lines(freqlist_path):
    """Count non-blank non-comment lines in the freqlist .txt."""
    if not os.path.exists(freqlist_path):
        raise CubeStructureError(f"Freqlist not found: {freqlist_path}")
    n = 0
    with open(freqlist_path) as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith('#'):
                continue
            n += 1
    return n


def validate_freqlist_against_cube(cube_path, freqlist_path):
    """Run validate_cube_structure() + check freq channel count.

    Returns the same dict as validate_cube_structure() with an extra key
    ``'freqlist_lines'``. Raises CubeStructureError on a count mismatch.
    """
    info = validate_cube_structure(cube_path)
    n_lines = count_freqlist_lines(freqlist_path)
    info['freqlist_lines'] = n_lines
    if n_lines != info['freq_nchans']:
        raise CubeStructureError(
            f"Frequency mismatch: cube '{cube_path}' has "
            f"{info['freq_nchans']} channels on its FREQ axis (NAXIS{info['freq_axis']}), "
            f"but '{freqlist_path}' contains {n_lines} entries. "
            "rmsynth3d requires one frequency per channel, in matching order."
        )
    return info


def transpose_cube_in_place(path, info=None):
    """Reorder a cube from (RA, DEC, STOKES, FREQ) -> (RA, DEC, FREQ, STOKES).

    Rewrites the file in place. Updates CTYPE/CRVAL/CRPIX/CDELT/CUNIT for
    axes 3 and 4 accordingly so downstream WCS readers see (FREQ, STOKES).
    No-op if ``info`` says no transpose is needed.

    Note: this is destructive. The caller should have already symlinked the
    cube into the workdir (rather than mutating the user's original on /idia/).
    Symlinks are followed by astropy's ``fits.open(..., mode='update')`` so
    rewriting works through the symlink.
    """
    from astropy.io import fits
    import numpy as np

    info = info or validate_cube_structure(path)
    if not info['needs_transpose']:
        return False

    with fits.open(path, mode='update') as hdul:
        hdu = hdul[0]
        data = hdu.data
        # Numpy axes are in reverse FITS order. FITS (1,2,3,4) = numpy (-1,-2,-3,-4).
        # We want FITS swap of axes 3<->4, which is numpy swap of -3 and -4 (i.e. 0 and 1
        # for a 4D array stored as numpy[STOKES, FREQ, DEC, RA] -> swap STOKES with FREQ).
        hdu.data = np.swapaxes(data, 0, 1)
        # Swap WCS keywords for axes 3 and 4
        for k in ('CTYPE', 'CRVAL', 'CRPIX', 'CDELT', 'CUNIT'):
            a, b = f'{k}3', f'{k}4'
            if a in hdu.header and b in hdu.header:
                hdu.header[a], hdu.header[b] = hdu.header[b], hdu.header[a]
        # Update NAXISn lengths to match the new ordering
        hdu.header['NAXIS3'], hdu.header['NAXIS4'] = (
            hdu.header['NAXIS4'], hdu.header['NAXIS3']
        )
        hdul.flush()
    return True


if __name__ == '__main__':
    import argparse, json
    p = argparse.ArgumentParser(description='Validate a FITS cube for processRM.')
    p.add_argument('cube')
    p.add_argument('--freqlist')
    p.add_argument('--transpose', action='store_true',
                   help='If axes are (RA,DEC,STOKES,FREQ), rewrite to (RA,DEC,FREQ,STOKES).')
    args = p.parse_args()
    info = (validate_freqlist_against_cube(args.cube, args.freqlist)
            if args.freqlist else validate_cube_structure(args.cube))
    print(json.dumps({k: v for k, v in info.items() if k != 'axes'}, indent=2))
    print('axes:', info['axes'])
    if args.transpose:
        changed = transpose_cube_in_place(args.cube, info)
        print(f"Transposed: {changed}")
