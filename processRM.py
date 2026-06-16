#!/usr/bin/env python3
"""
==================================================================
   ____                              ____  __  __
  |  _ \\ _ __ ___   ___ ___  ___ ___|  _ \\|  \\/  |
  | |_) | '__/ _ \\ / __/ _ \\/ __/ __| |_) | |\\/| |
  |  __/| | | (_) | (_|  __/\\__ \\__ \\  _ <| |  | |
  |_|   |_|  \\___/ \\___\\___||___/___/_| \\_\\_|  |_|

  processRM - RM Synthesis Pipeline Orchestrator
  RM-synthesis made simple
==================================================================

Modeled after processMeerKAT.

Usage:
  Generate a new config (you provide FITS file(s) + freq list):
    processRM -F mycube_IQUV.fits -f mycube.freqlist.txt
    processRM -F "mycube.stokesQ.fits mycube.stokesU.fits" -f freqs.txt

  Run an existing config:
    processRM -C myconfig.txt
    processRM -C myconfig.txt -s          # auto-submit pipeline

After generation/setup, run:
    ./submit_pipeline.sh                  # submits SLURM jobs
    ./fullSummary                         # check pipeline status
"""

__version__ = '1.0'

import argparse
import os
import sys
import shutil
import logging
from datetime import datetime
from time import gmtime

import config_parser


class ColoredFormatter(logging.Formatter):
    """Tint the level name green/orange/red; leave the message alone."""
    GREEN = '\033[92m'
    ORANGE = '\033[93m'
    RED = '\033[91m'
    RESET = '\033[0m'

    def format(self, record):
        original_level = record.levelname
        if original_level == 'INFO':
            record.levelname = f'{self.GREEN}INFO{self.RESET}'
        elif original_level == 'WARNING':
            record.levelname = f'{self.ORANGE}WARN{self.RESET}'
        elif original_level in ('ERROR', 'CRITICAL'):
            record.levelname = f'{self.RED}{original_level}{self.RESET}'
        formatted = super().format(record)
        record.levelname = original_level
        return formatted


logging.Formatter.converter = gmtime
logger = logging.getLogger(__name__)
_handler = logging.StreamHandler(stream=sys.stdout)  # stdout so it interleaves with print() output
_handler.setFormatter(ColoredFormatter(fmt="%(asctime)-15s %(levelname)s: %(message)s"))
logger.addHandler(_handler)
logger.setLevel(logging.INFO)
logger.propagate = False

THIS_PROG = os.path.realpath(__file__)
SCRIPT_DIR = os.path.dirname(THIS_PROG)
TEMPLATES_DIR = os.path.join(SCRIPT_DIR, 'templates')
AUX_DIR = os.path.join(SCRIPT_DIR, 'aux_scripts')
DEFAULT_CONFIG = os.path.join(SCRIPT_DIR, 'default_config.txt')
MASTER_SCRIPT = 'submit_pipeline.sh'

# ilifu-specific paths and settings
ILIFU_CONTAINER_DIR = '/idia/software/containers'
ILIFU_PROJECTS_DIR = '/idia/projects'
ILIFU_USERS_DIR = '/users'
ILIFU_DEFAULT_CONTAINER = '/idia/software/containers/casa-6.4.4-modular.simg'


def check_ilifu_environment():
    """
    Verify the user is running on the ilifu cluster.
    processRM is designed ONLY for ilifu and will not work elsewhere.
    """
    ilifu_indicators = [
        ILIFU_CONTAINER_DIR,
        ILIFU_USERS_DIR,
    ]
    found = [p for p in ilifu_indicators if os.path.exists(p)]

    if not found:
        logger.error("=" * 60)
        logger.error("processRM is designed ONLY for the ilifu cluster")
        logger.error("=" * 60)
        logger.error("This system does not appear to be on ilifu:")
        for p in ilifu_indicators:
            logger.error(f"  - {p}: NOT FOUND")
        logger.error("")
        logger.error("Please ssh into ilifu first:")
        logger.error("  ssh -A amani@slurm.ilifu.ac.za")
        logger.error("")
        sys.exit(1)

    # Check SLURM is available (sbatch command)
    if not shutil.which('sbatch'):
        logger.error("sbatch not found in PATH.")
        logger.error("processRM requires SLURM (only available on ilifu compute environment).")
        logger.error("If you are on the login node, ensure 'bash -l' is loaded.")
        sys.exit(1)

    logger.info(f"ilifu environment detected: {found[0]}")

# Pipeline template scripts (copied into working dir)
PIPELINE_SCRIPTS = [
    'create_subimage.py',
    'create_subimage_rmsy_cube.py',
    'run_parallel_rmsy.py',
    'merge_image_parts.py',
]

# Aux scripts (copied into working dir)
AUX_SCRIPTS = [
    'fullSummary',
]


def parse_args():
    """Parse command-line arguments for processRM."""
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Build a config from a full Stokes cube and frequency list
  processRM -F mycube_IQUV.fits -f mycube.freqlist.txt

  # Build a config from separate Q and U cubes
  processRM -F "mycube.stokesQ.fits mycube.stokesU.fits" -f freqs.txt

  # Use an existing config file (no Stokes extraction)
  processRM -C myconfig.txt

  # Build config AND submit pipeline immediately
  processRM -F mycube_IQUV.fits -f freqs.txt -s

  # Specify number of chunks (must be <= parallel in config)
  processRM -F mycube_IQUV.fits -f freqs.txt --chunks 50
        """
    )

    parser.add_argument('-C', '--config',
                        help='Path to existing config file')
    parser.add_argument('-F', '--fitsfile',
                        help='Path to FITS cube(s). Single full-Stokes IQUV cube '
                             '(e.g. mycube_IQUV.fits) OR two files in quotes '
                             '(e.g. "mycube.stokesQ.fits mycube.stokesU.fits"). '
                             'Relative or absolute paths are fine — processRM will '
                             'symlink them into the current directory so all outputs '
                             'land here.')
    parser.add_argument('-f', '--freqlist',
                        help='Path to frequency list (.txt). Required if -F is used.')
    parser.add_argument('-s', '--submit', action='store_true',
                        help='Auto-submit the pipeline after generation')
    parser.add_argument('--chunks', type=int,
                        help='Number of chunks to split image into (must be <= parallel in config)')
    parser.add_argument('--workdir', default=None,
                        help='Working directory (defaults to current directory)')
    parser.add_argument('--rm-container', dest='rm_container_override', default=None,
                        help='Override path to rm-env.sif (default: ~/processRM/container/rm-env.sif). '
                             'Use this if you moved the container to a project-shared location.')
    parser.add_argument('--version', action='version', version=f'processRM {__version__}')

    args = parser.parse_args()
    return args


def setup_workdir(workdir):
    """Create working directory and required subdirectories."""
    if workdir is None:
        workdir = os.getcwd()
    workdir = os.path.abspath(workdir)

    if not os.path.exists(workdir):
        os.makedirs(workdir)
        logger.info(f"Created working directory: {workdir}")

    # Create subdirectories
    for subdir in ['logs', 'processing', 'errors']:
        path = os.path.join(workdir, subdir)
        if not os.path.exists(path):
            os.makedirs(path)

    # Create error subdirectories for organization
    error_subdirs = ['extract', 'rmsynth', 'rmclean', 'merge']
    for subdir in error_subdirs:
        path = os.path.join(workdir, 'errors', subdir)
        if not os.path.exists(path):
            os.makedirs(path)

    return workdir


def copy_pipeline_scripts(workdir):
    """Copy template scripts into the working directory."""
    for script in PIPELINE_SCRIPTS:
        src = os.path.join(TEMPLATES_DIR, script)
        dst = os.path.join(workdir, script)
        if not os.path.exists(src):
            logger.warning(f"Template script not found: {src}")
            continue
        shutil.copy2(src, dst)
        os.chmod(dst, 0o755)
        logger.info(f"Copied: {script}")

    for script in AUX_SCRIPTS:
        src = os.path.join(AUX_DIR, script)
        dst = os.path.join(workdir, script)
        if not os.path.exists(src):
            logger.warning(f"Aux script not found: {src}")
            continue
        shutil.copy2(src, dst)
        os.chmod(dst, 0o755)
        logger.info(f"Copied: {script}")


def validate_ilifu_path(path, label="file"):
    """
    Validate that a path is on the ilifu filesystem.
    Warn if the path is outside expected ilifu mountpoints.
    """
    abs_path = os.path.abspath(path)
    ilifu_mounts = ['/idia/', '/users/', '/scratch/', '/home/']
    on_ilifu_fs = any(abs_path.startswith(m) for m in ilifu_mounts)
    if not on_ilifu_fs:
        logger.warning(f"  -> {label} path '{abs_path}' may not be on ilifu filesystem.")
        logger.warning("     Expected location like /idia/projects/... or /users/...")


def link_into_workdir(src_path, workdir, label="file"):
    """
    Ensure src_path is accessible by basename inside workdir.
    Returns the basename to use in the config.

    Pipeline scripts (run_parallel_rmsy.py / create_subimage.py) embed the input
    filename into chunk filenames (e.g. processing/part_1_<input>.im). If the
    user passes '../foo.fits' or an absolute path, that breaks. We sidestep this
    by symlinking the real file into workdir under its basename, and storing only
    the basename in the config. The pipeline then sees a simple local filename
    while the user keeps their data wherever it actually lives.
    """
    src_abs = os.path.abspath(src_path)
    basename = os.path.basename(src_abs)
    dst = os.path.join(workdir, basename)

    if os.path.abspath(dst) == src_abs:
        # Already in workdir — nothing to do
        return basename

    if os.path.lexists(dst):
        # Existing entry. If it's a symlink to the same target, fine; otherwise warn.
        try:
            if os.path.islink(dst) and os.readlink(dst) == src_abs:
                logger.info(f"  -> {label}: existing symlink already points to {src_abs}")
                return basename
        except OSError:
            pass
        logger.warning(f"  -> {label}: '{dst}' already exists and is not our symlink; using it as-is")
        return basename

    os.symlink(src_abs, dst)
    logger.info(f"  -> {label}: linked {dst} -> {src_abs}")
    return basename


def build_config_from_args(args, workdir):
    """
    Build a new config file based on -F (FITS file) and -f (freq list) arguments.
    Returns the path to the new config file.
    """
    if not args.freqlist:
        logger.error("-f/--freqlist is required when using -F/--fitsfile")
        sys.exit(1)

    if not os.path.exists(args.freqlist):
        logger.error(f"Frequency list not found: {args.freqlist}")
        sys.exit(1)

    validate_ilifu_path(args.freqlist, label="freqlist")

    # Parse FITS file(s) — could be one full cube or two split cubes
    fits_files = args.fitsfile.split()
    fits_full = ''
    fits_q = ''
    fits_u = ''

    if len(fits_files) == 1:
        fits_full_abs = os.path.abspath(fits_files[0])
        if not os.path.exists(fits_full_abs):
            logger.error(f"FITS file not found: {fits_full_abs}")
            sys.exit(1)
        validate_ilifu_path(fits_full_abs, label="fits_full")
        # Link into workdir so pipeline scripts can reference by basename
        fits_full = link_into_workdir(fits_full_abs, workdir, label="fits_full")
    elif len(fits_files) == 2:
        fits_q_abs = os.path.abspath(fits_files[0])
        fits_u_abs = os.path.abspath(fits_files[1])
        for f in [fits_q_abs, fits_u_abs]:
            if not os.path.exists(f):
                logger.error(f"FITS file not found: {f}")
                sys.exit(1)
            validate_ilifu_path(f, label="fits_stokesQ/U")
        fits_q = link_into_workdir(fits_q_abs, workdir, label="fits_stokesQ")
        fits_u = link_into_workdir(fits_u_abs, workdir, label="fits_stokesU")
    else:
        logger.error(
            "ERROR: -F expects 1 file (full Stokes cube) or 2 files (Q U). "
            f"Got {len(fits_files)} files."
        )
        sys.exit(1)

    freqlist_abs = os.path.abspath(args.freqlist)
    freqlist = link_into_workdir(freqlist_abs, workdir, label="freqlist")

    # Copy default config to workdir
    config_path = os.path.join(workdir, 'myconfig.txt')
    shutil.copy2(DEFAULT_CONFIG, config_path)
    logger.info(f"Created config file: {config_path}")

    # Update config with user-provided values
    taskvals, config = config_parser.parse_config(config_path)
    config.set('data', 'fits_full', f"'{fits_full}'")
    config.set('data', 'fits_stokesQ', f"'{fits_q}'")
    config.set('data', 'fits_stokesU', f"'{fits_u}'")
    config.set('data', 'freqlist', f"'{freqlist}'")

    if args.chunks:
        config.set('chunking', 'chunks', str(args.chunks))

    if args.submit:
        config.set('slurm', 'submit', 'True')

    # Resolve rm_container path: --rm-container > current value > default location
    default_rm_container = os.path.join(SCRIPT_DIR, 'container', 'rm-env.sif')
    if args.rm_container_override:
        rm_container = os.path.abspath(os.path.expanduser(args.rm_container_override))
    else:
        existing = config.get('slurm', 'rm_container', fallback="''").strip("'\"")
        rm_container = existing or default_rm_container
    config.set('slurm', 'rm_container', f"'{rm_container}'")

    if not os.path.exists(rm_container):
        logger.warning(f"  -> rm_container path does not exist yet: {rm_container}")
        logger.warning("     If you haven't run setup.sh, do so to fetch the container.")
        logger.warning("     Or pass --rm-container <path> to point at an existing .sif.")

    with open(config_path, 'w') as f:
        config.write(f)

    return config_path


def generate_submit_script(workdir, config_path):
    """Generate the submit_pipeline.sh master script."""
    submit_path = os.path.join(workdir, MASTER_SCRIPT)

    script_content = f"""#!/bin/bash
# ==================================================================
#  processRM master submission script
# ==================================================================
# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
# Config: {config_path}

set -e
CONFIG="{os.path.basename(config_path)}"
WORKDIR="{workdir}"
cd "$WORKDIR"

echo "=================================================="
echo "  processRM Pipeline Submission"
echo "=================================================="
echo "Workdir: $WORKDIR"
echo "Config:  $CONFIG"
echo ""

# Parse key config values
FITS_FULL=$(python3 -c "
import config_parser
t,_ = config_parser.parse_config('$CONFIG')
print(t['data'].get('fits_full', ''))
")
FITS_Q=$(python3 -c "
import config_parser
t,_ = config_parser.parse_config('$CONFIG')
print(t['data'].get('fits_stokesQ', ''))
")
FITS_U=$(python3 -c "
import config_parser
t,_ = config_parser.parse_config('$CONFIG')
print(t['data'].get('fits_stokesU', ''))
")
FREQLIST=$(python3 -c "
import config_parser
t,_ = config_parser.parse_config('$CONFIG')
print(t['data'].get('freqlist', ''))
")
PARALLEL=$(python3 -c "
import config_parser
t,_ = config_parser.parse_config('$CONFIG')
print(t['chunking'].get('parallel', 100))
")
CHUNKS=$(python3 -c "
import config_parser
t,_ = config_parser.parse_config('$CONFIG')
print(t['chunking'].get('chunks', t['chunking'].get('parallel', 100)))
")
ACCOUNT=$(python3 -c "
import config_parser
t,_ = config_parser.parse_config('$CONFIG')
print(t['slurm'].get('account', 'b03-idia-ag'))
")
RM_CONTAINER=$(python3 -c "
import config_parser
t,_ = config_parser.parse_config('$CONFIG')
print(t['slurm'].get('rm_container', ''))
")
CASA_CONTAINER=$(python3 -c "
import config_parser
t,_ = config_parser.parse_config('$CONFIG')
print(t['slurm'].get('casa_container', ''))
")

# Validate containers (required — no host venv fallback)
if [ -z "$RM_CONTAINER" ]; then
    echo "ERROR: [slurm] rm_container is not set in config."
    echo "  Build the container first: see processRM/container/README.md"
    exit 1
fi
if [ ! -f "$RM_CONTAINER" ]; then
    echo "ERROR: rm_container not found: $RM_CONTAINER"
    echo "  Download it with: cd ~/processRM/container && ./download_container.sh"
    exit 1
fi
if [ ! -f "$CASA_CONTAINER" ]; then
    echo "ERROR: casa_container not found: $CASA_CONTAINER"
    exit 1
fi

echo "RM container:   $RM_CONTAINER"
echo "CASA container: $CASA_CONTAINER"
THRESHOLD=$(python3 -c "
import config_parser
t,_ = config_parser.parse_config('$CONFIG')
print(t['rmclean'].get('threshold', 0.0000010))
")
ITERATIONS=$(python3 -c "
import config_parser
t,_ = config_parser.parse_config('$CONFIG')
print(t['rmclean'].get('iterations', 5000))
")

# Stage 1: Extract Stokes Q/U if user provided full cube
if [ -n "$FITS_FULL" ]; then
    echo "[Stage 1] Extracting Stokes Q/U from full cube..."
    ./create_subimage.py --inputcube "$FITS_FULL"
    BASENAME=$(basename "$FITS_FULL" .fits)
    FITS_Q="${{BASENAME}}.stokesQ.fits"
    FITS_U="${{BASENAME}}.stokesU.fits"
    echo "  -> Created: $FITS_Q"
    echo "  -> Created: $FITS_U"
else
    echo "[Stage 1] Skipping extraction (Q and U already provided)"
fi

# Stage 2: Generate RM synthesis sbatch + submit
echo ""
echo "[Stage 2] Generating RM synthesis sbatch file..."
./run_parallel_rmsy.py --parallel "$CHUNKS" \\
    --inputFitsStokesQ "$FITS_Q" \\
    --inputFitsStokesU "$FITS_U" \\
    --freqList "$FREQLIST" \\
    --rmsyCleanThrethold "$THRESHOLD" \\
    --rmsyCleanIterations "$ITERATIONS" \\
    --account "$ACCOUNT" \\
    --casaContainer "$CASA_CONTAINER" \\
    --rmContainer "$RM_CONTAINER" \\
    --createSbatch

echo ""
echo "[Stage 3] Submitting RM synthesis jobs to SLURM..."
SLURMID_RMSY=$(sbatch run_parallel_rmsy.sbatch | cut -d ' ' -f4)
echo "  -> Submitted RM synthesis: SLURM job $SLURMID_RMSY"

# Stage 4: Submit merge job with dependency on RM synthesis
echo ""
echo "[Stage 4] Generating and submitting merge job..."
INPUT_CUBE="${{FITS_FULL:-$FITS_Q}}"
./merge_image_parts.py --inputcube "$INPUT_CUBE" --account "$ACCOUNT" \\
    --rmContainer "$RM_CONTAINER" --casaContainer "$CASA_CONTAINER" &
sleep 5

if [ -f merge_image_parts.sbatch ]; then
    SLURMID_MERGE=$(sbatch --dependency=afterok:$SLURMID_RMSY merge_image_parts.sbatch | cut -d ' ' -f4)
    echo "  -> Submitted merge: SLURM job $SLURMID_MERGE (depends on $SLURMID_RMSY)"
fi

echo ""
echo "===================================="
echo "  Pipeline submitted successfully!"
echo "===================================="
echo "Check status with: ./fullSummary"
echo "View logs in: logs/"
echo "View errors in: errors/"
"""

    with open(submit_path, 'w') as f:
        f.write(script_content)
    os.chmod(submit_path, 0o755)
    logger.info(f"Generated: {submit_path}")
    return submit_path


BANNER = r"""
==================================================================
   ____                              ____  __  __
  |  _ \ _ __ ___   ___ ___  ___ ___|  _ \|  \/  |
  | |_) | '__/ _ \ / __/ _ \/ __/ __| |_) | |\/| |
  |  __/| | | (_) | (_|  __/\__ \__ \  _ <| |  | |
  |_|   |_|  \___/ \___\___||___/___/_| \_\_|  |_|

  processRM - RM Synthesis Pipeline Orchestrator
  RM-synthesis made simple
==================================================================
"""

def main():
    print(BANNER, flush=True)
    args = parse_args()

    # Ilifu environment check (processRM is ilifu-only)
    check_ilifu_environment()

    workdir = setup_workdir(args.workdir)

    print()
    logger.info(f"processRM v{__version__}")
    logger.info(f"Working directory: {workdir}")
    print()

    # Case 1: User provided -C (existing config)
    if args.config:
        config_path = os.path.abspath(args.config)
        if not os.path.exists(config_path):
            logger.error(f"Config file not found: {config_path}")
            sys.exit(1)
        logger.info(f"Using config: {config_path}")

    # Case 2: User provided -F (build config from FITS file)
    elif args.fitsfile:
        config_path = build_config_from_args(args, workdir)

    else:
        logger.error("Must provide either -C (config file) or -F (FITS file)")
        sys.exit(1)

    # Validate config
    try:
        config_parser.validate_config(config_path)
        logger.info("Config validated successfully.")
    except (ValueError, KeyError, FileNotFoundError) as e:
        logger.error(f"Config validation failed: {e}")
        sys.exit(1)

    print()

    # Copy scripts and generate submit_pipeline.sh
    copy_pipeline_scripts(workdir)
    # Also copy config_parser into workdir so submit_pipeline.sh can use it
    shutil.copy2(os.path.join(SCRIPT_DIR, 'config_parser.py'),
                 os.path.join(workdir, 'config_parser.py'))
    submit_script = generate_submit_script(workdir, config_path)

    # Auto-submit if -s flag was used
    if args.submit:
        print()
        logger.info("Auto-submitting pipeline (-s flag)...")
        os.system(f"cd {workdir} && {submit_script}")
    else:
        print("\n" + "=" * 50)
        print("  processRM setup complete!")
        print("=" * 50)
        print(f"  Workdir:    {workdir}")
        print(f"  Config:     {config_path}")
        print()
        print(f"  To submit the pipeline, run:")
        print(f"     ./submit_pipeline.sh")
        print()
        print(f"  To check status:")
        print(f"     ./fullSummary")
        print("=" * 50)
        print()


if __name__ == '__main__':
    main()
