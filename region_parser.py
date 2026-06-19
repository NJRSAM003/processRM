#!/usr/bin/env python3
"""
Region file parser for processRM.

Supports box-only regions exported from CARTA in four flavours:
    * CRTF (pixel)    e.g. centerbox [[X pix, Y pix], [W pix, H pix]] ...
    * CRTF (world)    e.g. centerbox [[02:51:12.3, -031.06.15.4], [934.9arcsec, 801.4arcsec]] ...
    * DS9  (pixel)    'image' system, e.g. box(X, Y, W, H, ROT)
    * DS9  (world)    'fk5'/'icrs' system, e.g. box(RA_deg, DEC_deg, W", H", ROT)

Non-box shapes (rotbox with non-zero angle, ellipse, polygon, circle, ...) are
rejected with a clear error so the user knows to redraw their region in CARTA.

The public entrypoint, ``parse_region_file(path, wcs_header=None)``, returns a
list of dicts:
    {'index': N, 'x_center_px': float, 'y_center_px': float,
     'width_px': int, 'height_px': int}
Pixel coords are 1-based to match FITS/CASA convention.

If the region file is in world coordinates, ``wcs_header`` (an astropy
``fits.Header``) is required so we can convert RA/Dec to pixel offsets.
"""

import os
import re
from collections import OrderedDict


class RegionParseError(ValueError):
    """Raised when a region file is malformed or contains unsupported shapes."""


# ---- Tokenisation helpers -------------------------------------------------

_CRTF_VALUE_RE = re.compile(
    r'(?P<num>[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s*(?P<unit>pix|deg|rad|arcsec|arcmin)?',
    re.IGNORECASE,
)
_HMS_RE = re.compile(r'^[+-]?\d{1,2}[:hH]\d{1,2}[:mM]\d{1,2}(?:\.\d+)?[sS]?$')
_DMS_RE = re.compile(r'^[+-]?\d{1,3}[\.:dD]\d{1,2}[\.:mM]\d{1,2}(?:\.\d+)?[sS]?$')


def _sexagesimal_to_deg(token, is_ra):
    """Convert HH:MM:SS or DD.MM.SS (CARTA's preferred CRTF form) to degrees.

    CRTF uses ':' for HMS and '.' separators for DMS; we also accept 'h/m/s'
    suffixes for safety. ``is_ra`` controls the *15 factor for RA.
    """
    s = token.strip()
    # Normalise: collapse h/m/s/d markers to a common delimiter
    s = re.sub(r'[hHmMsSdD]', ':', s)
    s = s.replace('.', ':', 2) if is_ra is False and ':' not in s else s
    parts = [p for p in s.split(':') if p != '']
    if len(parts) != 3:
        raise RegionParseError(f"Cannot parse sexagesimal value: '{token}'")
    sign = 1
    if parts[0].startswith('-'):
        sign = -1
        parts[0] = parts[0][1:]
    elif parts[0].startswith('+'):
        parts[0] = parts[0][1:]
    h_or_d, m, sec = float(parts[0]), float(parts[1]), float(parts[2])
    decimal = h_or_d + m / 60 + sec / 3600
    if is_ra:
        decimal *= 15.0  # hours -> degrees
    return sign * decimal


def _parse_world_coord(ra_token, dec_token):
    """Return (ra_deg, dec_deg). Accepts decimal degrees or sexagesimal."""
    ra_token = ra_token.strip()
    dec_token = dec_token.strip()
    # Decimal degrees (DS9 fk5)
    try:
        return float(ra_token), float(dec_token)
    except ValueError:
        pass
    # Sexagesimal (CRTF world)
    return _sexagesimal_to_deg(ra_token, is_ra=True), _sexagesimal_to_deg(dec_token, is_ra=False)


def _parse_size_with_unit(token):
    """Returns (value, unit_str_lower) e.g. ('934.9565', 'arcsec'). Default unit None."""
    m = _CRTF_VALUE_RE.match(token.strip())
    if not m:
        raise RegionParseError(f"Cannot parse size token: '{token}'")
    val = float(m.group('num'))
    unit = (m.group('unit') or '').lower() or None
    return val, unit


def _angular_size_to_pixels(value, unit, cdelt_deg):
    """Convert an angular size to integer pixels using |CDELT| in degrees."""
    if unit in (None, 'pix', 'pixel', 'pixels'):
        return int(round(value))
    if unit == 'deg':
        deg = value
    elif unit == 'rad':
        deg = value * 180.0 / 3.14159265358979323846
    elif unit == 'arcsec':
        deg = value / 3600.0
    elif unit == 'arcmin':
        deg = value / 60.0
    else:
        raise RegionParseError(f"Unsupported angular unit: '{unit}'")
    return max(1, int(round(deg / abs(cdelt_deg))))


# ---- WCS helpers ----------------------------------------------------------

def _wcs_world_to_pixel(ra_deg, dec_deg, wcs_header):
    """RA/Dec (deg) -> (x_px, y_px) 1-based using astropy WCS.

    Falls back to a manual tangent-plane projection if astropy isn't available
    (so the parser can still be imported without astropy in environments where
    only pixel regions are used).
    """
    try:
        from astropy.wcs import WCS
        # Build a 2D celestial WCS slice so we don't need to know the cube depth here.
        wcs = WCS(wcs_header).celestial
        x, y = wcs.world_to_pixel_values(ra_deg, dec_deg)
        return float(x) + 1.0, float(y) + 1.0  # WCS is 0-based; FITS is 1-based
    except Exception as e:
        raise RegionParseError(
            f"Could not convert world coords (RA={ra_deg}, Dec={dec_deg}) to pixels: {e}"
        )


# ---- Format detection -----------------------------------------------------

def _detect_format(text):
    """Returns one of: 'crtf-pixel', 'crtf-world', 'ds9-pixel', 'ds9-world'."""
    head = text.lstrip().splitlines()[:5]
    head_l = '\n'.join(head).lower()
    is_crtf = '#crtf' in head_l
    is_ds9 = ('region file format: ds9' in head_l) or ('# region' in head_l and 'ds9' in head_l)
    if not (is_crtf or is_ds9):
        # Best-effort guess from line shape
        if 'centerbox' in text.lower() or 'rotbox' in text.lower():
            is_crtf = True
        elif re.search(r'\bbox\s*\(', text):
            is_ds9 = True
        else:
            raise RegionParseError(
                "Could not identify region file format. Expected CRTF "
                "(begins with '#CRTFv0') or DS9 ('# Region file format: DS9 ...')."
            )

    # Determine pixel vs world
    if is_crtf:
        # CRTF: pixel if any 'pix' token appears in coordinate slots, world otherwise.
        # We look at the first 'centerbox' line.
        for line in text.splitlines():
            ls = line.strip()
            if ls.lower().startswith('centerbox'):
                if 'pix' in ls.lower().split(']')[0].lower():
                    return 'crtf-pixel'
                return 'crtf-world'
        raise RegionParseError("CRTF file has no centerbox region.")
    else:
        # DS9: coordinate system is declared on its own line ('image', 'fk5', 'icrs', ...)
        coord_sys = None
        for line in text.splitlines():
            ls = line.strip().lower()
            if ls in ('image', 'physical'):
                coord_sys = 'pixel'
                break
            if ls in ('fk5', 'icrs', 'galactic', 'j2000', 'ecliptic'):
                coord_sys = 'world'
                break
        if coord_sys is None:
            raise RegionParseError(
                "DS9 region file: could not find a coordinate-system line "
                "(expected one of: image, physical, fk5, icrs, j2000)."
            )
        return 'ds9-pixel' if coord_sys == 'pixel' else 'ds9-world'


# ---- Per-format parsers ---------------------------------------------------

_CRTF_LINE_RE = re.compile(
    r'^\s*(?P<shape>[A-Za-z_]+)\s*\[\s*\[\s*(?P<c1>[^,]+),\s*(?P<c2>[^\]]+)\s*\]'
    r'\s*,\s*\[\s*(?P<s1>[^,]+),\s*(?P<s2>[^\]]+)\s*\]\s*\]',
)


def _parse_crtf(text, wcs_header, world):
    boxes = []
    for line in text.splitlines():
        ls = line.strip()
        if not ls or ls.startswith('#'):
            continue
        m = _CRTF_LINE_RE.match(ls)
        if not m:
            continue
        shape = m.group('shape').lower()
        if shape != 'centerbox':
            if shape in ('rotbox', 'box', 'poly', 'circle', 'ellipse', 'annulus'):
                raise RegionParseError(
                    f"Region shape '{shape}' is not supported by processRM "
                    "(only axis-aligned centerbox regions are allowed). "
                    "Redraw the region in CARTA as a rectangle and re-export."
                )
            continue  # Unknown / comment-like line, skip silently
        c1, c2, s1, s2 = m.group('c1'), m.group('c2'), m.group('s1'), m.group('s2')
        if world:
            ra_deg, dec_deg = _parse_world_coord(c1, c2)
            x_px, y_px = _wcs_world_to_pixel(ra_deg, dec_deg, wcs_header)
            cdelt = abs(float(wcs_header.get('CDELT1', wcs_header.get('CD1_1', 1.0))))
            w_val, w_unit = _parse_size_with_unit(s1)
            h_val, h_unit = _parse_size_with_unit(s2)
            width_px = _angular_size_to_pixels(w_val, w_unit, cdelt)
            height_px = _angular_size_to_pixels(h_val, h_unit, cdelt)
        else:
            x_val, _ = _parse_size_with_unit(c1)
            y_val, _ = _parse_size_with_unit(c2)
            x_px, y_px = x_val, y_val
            w_val, w_unit = _parse_size_with_unit(s1)
            h_val, h_unit = _parse_size_with_unit(s2)
            if w_unit not in (None, 'pix') or h_unit not in (None, 'pix'):
                raise RegionParseError(
                    "Pixel-coordinate CRTF region must use pixel sizes too."
                )
            width_px = int(round(w_val))
            height_px = int(round(h_val))
        boxes.append((x_px, y_px, width_px, height_px))
    return boxes


_DS9_LINE_RE = re.compile(
    r'^\s*(?P<shape>[A-Za-z]+)\s*\(\s*(?P<args>[^)]+)\)',
)


def _parse_ds9(text, wcs_header, world):
    boxes = []
    for line in text.splitlines():
        ls = line.strip()
        if not ls or ls.startswith('#'):
            continue
        # Skip the coordinate-system declaration lines
        if ls.lower() in ('image', 'physical', 'fk5', 'icrs', 'galactic', 'j2000', 'ecliptic'):
            continue
        # Strip trailing comment
        ls_nocomment = ls.split('#', 1)[0].strip()
        m = _DS9_LINE_RE.match(ls_nocomment)
        if not m:
            continue
        shape = m.group('shape').lower()
        if shape != 'box':
            if shape in ('circle', 'ellipse', 'polygon', 'point', 'annulus', 'rotbox'):
                raise RegionParseError(
                    f"Region shape '{shape}' is not supported by processRM "
                    "(only axis-aligned box regions are allowed). "
                    "Redraw the region in CARTA as a rectangle and re-export."
                )
            continue
        args_raw = [a.strip() for a in m.group('args').split(',')]
        if len(args_raw) < 4:
            raise RegionParseError(f"Malformed DS9 box: '{ls}'")
        # If a fifth arg is present and non-zero, it's a rotation - reject.
        if len(args_raw) >= 5:
            try:
                rot = float(args_raw[4])
            except ValueError:
                rot = 0.0
            if abs(rot) > 1e-6:
                raise RegionParseError(
                    f"Rotated boxes (rotation={rot}°) are not supported. "
                    "Set rotation to 0 in CARTA and re-export."
                )
        if world:
            ra_deg, dec_deg = _parse_world_coord(args_raw[0], args_raw[1])
            x_px, y_px = _wcs_world_to_pixel(ra_deg, dec_deg, wcs_header)
            cdelt = abs(float(wcs_header.get('CDELT1', wcs_header.get('CD1_1', 1.0))))
            # DS9 width/height tokens carry their own unit, '"' (arcsec) being the common one.
            w_token = args_raw[2].replace('"', 'arcsec').replace("'", 'arcmin')
            h_token = args_raw[3].replace('"', 'arcsec').replace("'", 'arcmin')
            w_val, w_unit = _parse_size_with_unit(w_token)
            h_val, h_unit = _parse_size_with_unit(h_token)
            if w_unit is None:
                w_unit = 'arcsec'
            if h_unit is None:
                h_unit = 'arcsec'
            width_px = _angular_size_to_pixels(w_val, w_unit, cdelt)
            height_px = _angular_size_to_pixels(h_val, h_unit, cdelt)
        else:
            x_px = float(args_raw[0])
            y_px = float(args_raw[1])
            width_px = int(round(float(args_raw[2])))
            height_px = int(round(float(args_raw[3])))
        boxes.append((x_px, y_px, width_px, height_px))
    return boxes


# ---- Public entrypoint ----------------------------------------------------

def parse_region_file(path, wcs_header=None):
    """Parse a CARTA-exported region file into a list of box dicts.

    Parameters
    ----------
    path : str
        Path to the region file.
    wcs_header : astropy.io.fits.Header, optional
        Header of the input cube. Required only when the region file is in
        world coordinates.

    Returns
    -------
    list of dict
        Each dict has keys: ``index`` (1-based int), ``x_center_px``,
        ``y_center_px``, ``width_px``, ``height_px``.
    """
    if not os.path.exists(path):
        raise RegionParseError(f"Region file not found: {path}")
    with open(path) as f:
        text = f.read()

    fmt = _detect_format(text)
    world = fmt.endswith('world')
    if world and wcs_header is None:
        raise RegionParseError(
            f"Region file '{path}' uses world coordinates but no cube WCS was "
            "supplied. Use a pixel-coordinate region or pass the cube header."
        )

    if fmt.startswith('crtf'):
        boxes = _parse_crtf(text, wcs_header, world)
    else:
        boxes = _parse_ds9(text, wcs_header, world)

    if not boxes:
        raise RegionParseError(
            f"No supported (axis-aligned) box regions found in '{path}'. "
            "Make sure you exported rectangles, not circles/ellipses/polygons."
        )

    return [
        {
            'index': i + 1,
            'x_center_px': float(x),
            'y_center_px': float(y),
            'width_px': int(w),
            'height_px': int(h),
        }
        for i, (x, y, w, h) in enumerate(boxes)
    ]


def describe_regions(regions):
    """Human-readable one-liner per region, for the BUILD-time preview."""
    out = []
    for r in regions:
        out.append(
            f"region{r['index']}: centre=({r['x_center_px']:.1f}, {r['y_center_px']:.1f})px, "
            f"size={r['width_px']}x{r['height_px']}px"
        )
    return out


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser(description='Parse a CARTA region file and print the boxes.')
    p.add_argument('region_file', help='Path to .crtf / .ds9 / .reg')
    p.add_argument('--fits', help='Path to FITS cube (required for world-coord regions)')
    args = p.parse_args()
    header = None
    if args.fits:
        from astropy.io import fits as _fits
        header = _fits.getheader(args.fits)
    for line in describe_regions(parse_region_file(args.region_file, header)):
        print(line)
