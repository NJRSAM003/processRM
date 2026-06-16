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
  BUILD a new config (you provide FITS file(s) + freq list):
    processRM -F mycube_IQUV.fits -f mycube.freqlist.txt
    processRM -F "mycube.stokesQ.fits mycube.stokesU.fits" -f freqs.txt
    processRM -F mycube_IQUV.fits -f freqs.txt -C run1.txt   # custom name

  RUN an existing config:
    processRM -R myconfig.txt
    processRM -R myconfig.txt -s          # also auto-submit

After build/setup, run:
    ./submit_pipeline.sh                  # submits SLURM jobs
    ./fullSummary                         # check pipeline status
"""

__version__ = '1.0'

import argparse
import os
import sys
import shutil
import logging
import re
from datetime import datetime
from time import gmtime

import config_parser


def update_config_inplace(config_path, updates):
    """Update specific keys in an INI file while preserving comments, blank lines,
    and column alignment of inline comments.

    Python's configparser.write() drops all inline comments, which would destroy
    the per-parameter guidance baked into default_config.txt. This function does
    a line-by-line rewrite that touches only the value of targeted keys.

    updates: dict mapping (section, key) -> new value (string, already quoted/formatted)
    """
    with open(config_path) as f:
        lines = f.readlines()

    # group 1 = indent, 2 = key, 3 = ' = ' (with surrounding whitespace),
    # 4 = value (greedy stop at whitespace+#), 5 = trailing whitespace, 6 = comment, 7 = newline
    line_re = re.compile(
        r'^(\s*)([A-Za-z_]\w*)(\s*=\s*)(.*?)([ \t]*)(#.*)?(\r?\n?)$'
    )
    section_re = re.compile(r'^\s*\[([^\]]+)\]')

    current_section = None
    out = []

    for line in lines:
        sec_match = section_re.match(line)
        if sec_match:
            current_section = sec_match.group(1)
            out.append(line)
            continue

        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            out.append(line)
            continue

        m = line_re.match(line)
        if not (m and current_section):
            out.append(line)
            continue

        indent, key, eq, old_value, gap, comment, newline = m.groups()
        comment = comment or ''
        gap = gap or ''

        target = (current_section, key)
        if target not in updates:
            out.append(line)
            continue

        new_value = updates[target]

        if comment:
            # Keep the comment at its original column for alignment
            original_value_end = len(indent) + len(key) + len(eq) + len(old_value)
            comment_col = original_value_end + len(gap)
            new_value_end = len(indent) + len(key) + len(eq) + len(new_value)
            needed_gap = max(comment_col - new_value_end, 1)
            out.append(f"{indent}{key}{eq}{new_value}{' ' * needed_gap}{comment}{newline}")
        else:
            out.append(f"{indent}{key}{eq}{new_value}{newline}")

    with open(config_path, 'w') as f:
        f.writelines(out)


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
  # BUILD a config from a full Stokes cube + freq list (default name: myconfig.txt)
  processRM -F mycube_IQUV.fits -f mycube.freqlist.txt

  # BUILD with a custom config name
  processRM -F mycube_IQUV.fits -f mycube.freqlist.txt -C run1.txt

  # BUILD from separate Q and U cubes
  processRM -F "mycube.stokesQ.fits mycube.stokesU.fits" -f freqs.txt

  # BUILD with a specific chunk count
  processRM -F mycube_IQUV.fits -f freqs.txt --chunks 50

  # BUILD and submit immediately
  processRM -F mycube_IQUV.fits -f freqs.txt -s

  # RUN an existing config (regenerate submit_pipeline.sh from it)
  processRM -R myconfig.txt

  # RUN and submit immediately
  processRM -R myconfig.txt -s
"""
    )

    parser.add_argument('-F', '--fitsfile',
                        help='BUILD mode: path to FITS cube(s). Single full-Stokes IQUV '
                             'cube (e.g. mycube_IQUV.fits) OR two files in quotes '
                             '(e.g. "mycube.stokesQ.fits mycube.stokesU.fits"). '
                             'Relative or absolute paths are fine — processRM will '
                             'symlink them into the current directory so all outputs '
                             'land here.')
    parser.add_argument('-f', '--freqlist',
                        help='Path to frequency list (.txt). Required when -F is used.')
    parser.add_argument('-C', '--config',
                        help='Optional name for the config built from -F '
                             '(default: myconfig.txt).')
    parser.add_argument('-R', '--run',
                        dest='run_config',
                        help='RUN mode: load an existing config file and regenerate '
                             'submit_pipeline.sh from it (no FITS inputs needed).')
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
    BUILD mode: write a config file from -F / -f / etc. and exit.
    No symlinks are created, no pipeline scripts are copied, no
    submit_pipeline.sh is generated. The config records ABSOLUTE paths
    to the input files; the RUN mode (-R) is what materialises symlinks
    and scaffolding when the user is ready to actually launch.
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
        fits_full = os.path.abspath(fits_files[0])
        if not os.path.exists(fits_full):
            logger.error(f"FITS file not found: {fits_full}")
            sys.exit(1)
        validate_ilifu_path(fits_full, label="fits_full")
    elif len(fits_files) == 2:
        fits_q = os.path.abspath(fits_files[0])
        fits_u = os.path.abspath(fits_files[1])
        for f in [fits_q, fits_u]:
            if not os.path.exists(f):
                logger.error(f"FITS file not found: {f}")
                sys.exit(1)
            validate_ilifu_path(f, label="fits_stokesQ/U")
    else:
        logger.error(
            "ERROR: -F expects 1 file (full Stokes cube) or 2 files (Q U). "
            f"Got {len(fits_files)} files."
        )
        sys.exit(1)

    freqlist = os.path.abspath(args.freqlist)

    # Copy default config to workdir under user-chosen name (or 'myconfig.txt')
    config_name = args.config if args.config else 'myconfig.txt'
    if not config_name.endswith('.txt'):
        config_name += '.txt'
    config_path = os.path.join(workdir, os.path.basename(config_name))
    if os.path.exists(config_path):
        logger.warning(f"Overwriting existing config: {config_path}")
    shutil.copy2(DEFAULT_CONFIG, config_path)
    logger.info(f"Created config file: {config_path}")

    # Use the parsed default config to determine the rm_container fallback,
    # then write all updates back via the format-preserving inline updater so
    # the per-parameter comments in default_config.txt survive.
    taskvals, _ = config_parser.parse_config(config_path)

    updates = {
        ('data', 'fits_full'):     f"'{fits_full}'",
        ('data', 'fits_stokesQ'):  f"'{fits_q}'",
        ('data', 'fits_stokesU'):  f"'{fits_u}'",
        ('data', 'freqlist'):      f"'{freqlist}'",
    }

    if args.chunks:
        updates[('chunking', 'chunks')] = str(args.chunks)

    if args.submit:
        updates[('slurm', 'submit')] = 'True'

    # Resolve rm_container path: --rm-container > current value in config > install default
    default_rm_container = os.path.join(SCRIPT_DIR, 'container', 'rm-env.sif')
    if args.rm_container_override:
        rm_container = os.path.abspath(os.path.expanduser(args.rm_container_override))
    else:
        existing = (taskvals.get('slurm', {}).get('rm_container') or '').strip()
        rm_container = existing or default_rm_container
    updates[('slurm', 'rm_container')] = f"'{rm_container}'"

    if not os.path.exists(rm_container):
        logger.warning(f"  -> rm_container path does not exist yet: {rm_container}")
        logger.warning("     If you haven't run setup.sh, do so to fetch the container.")
        logger.warning("     Or pass --rm-container <path> to point at an existing .sif.")

    update_config_inplace(config_path, updates)

    # [CHANGE 2026-06-16]: Geometry preview
    # The chunker uses int(NAXIS2 / parallel) which truncates, and the last chunk
    # absorbs the remainder. Print that math up front so the user can sanity-check
    # before submitting a 100-task array.
    _preview_chunk_geometry(args, fits_full, fits_q, workdir, taskvals)

    return config_path


def _preview_chunk_geometry(args, fits_full, fits_q, workdir, taskvals):
    """Read NAXIS2 from the input cube and log how it will be split."""
    try:
        from astropy.io import fits
    except ImportError:
        return
    # Config now stores absolute paths (build-only mode), so use them directly.
    sample_path = fits_full or fits_q
    if not sample_path or not os.path.exists(sample_path):
        return
    try:
        naxis2 = fits.getheader(sample_path)['NAXIS2']
    except Exception:
        return

    parallel = int(taskvals.get('chunking', {}).get('parallel', 100))
    chunks = int(args.chunks) if args.chunks else \
             int(taskvals.get('chunking', {}).get('chunks', parallel))
    y_size = naxis2 // parallel
    residual = naxis2 - parallel * y_size
    last_size = y_size + residual

    logger.info(
        f"chunk geometry: NAXIS2={naxis2}, parallel={parallel}, "
        f"y_size={y_size}px, last chunk={last_size}px (residual={residual}px absorbed)"
    )
    if chunks < parallel:
        logger.warning(
            f"  -> chunks ({chunks}) < parallel ({parallel}): only {chunks} tasks will run, "
            f"covering {chunks * y_size} of {naxis2} rows. Outputs may be incomplete."
        )


def generate_submit_script(workdir, config_path):
    """Generate the orchestrator sbatch and the thin submit_pipeline.sh wrapper.

    Mirrors processMeerKAT's design: submit_pipeline.sh only calls 'sbatch'
    (works from the login node). The actual orchestration (Stokes extraction,
    run_parallel_rmsy sbatch generation + sub-submission, merge sbatch
    generation + sub-submission) lives inside processRM_orchestrate.sbatch,
    which SLURM dispatches to a compute node where 'singularity exec' is
    permitted.
    """
    orch_path = os.path.join(workdir, 'processRM_orchestrate.sbatch')
    submit_path = os.path.join(workdir, MASTER_SCRIPT)

    # ----- orchestrator sbatch (runs on a compute node) -----
    orchestrator = rf"""#!/bin/bash
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=10GB
#SBATCH --job-name=processRM_orchestrate
#SBATCH --output=logs/orchestrate-%j.out
#SBATCH --error=logs/orchestrate-%j.err
#SBATCH --partition=Main
#SBATCH --time=02:00:00
# Account, container & config values are templated in below at generation time:
#SBATCH --account=__ACCOUNT__

set -e
export PYTHONDONTWRITEBYTECODE=1   # keep __pycache__ out of the workdir
CONFIG="{os.path.basename(config_path)}"
WORKDIR="{workdir}"
cd "$WORKDIR"

cat /etc/hostname
echo "=================================================="
echo "  processRM Orchestrator"
echo "=================================================="
echo "Workdir: $WORKDIR"
echo "Config:  $CONFIG"
echo ""

read_cfg () {{
    singularity --quiet exec __RM_CONTAINER__ python3 -c "
import config_parser
t,_ = config_parser.parse_config('$CONFIG')
v = t.get('$1', {{}}).get('$2', '')
s = '' if v is None else str(v).strip().strip(\"'\\\"\")
print(s or '$3')
"
}}

FITS_FULL=$(read_cfg data fits_full '')
FITS_Q=$(read_cfg data fits_stokesQ '')
FITS_U=$(read_cfg data fits_stokesU '')
FREQLIST=$(read_cfg data freqlist '')
PARALLEL=$(read_cfg chunking parallel 100)
CHUNKS=$(read_cfg chunking chunks $PARALLEL)
ACCOUNT=$(read_cfg slurm account b03-idia-ag)
RM_CONTAINER=$(read_cfg slurm rm_container '')
CASA_CONTAINER=$(read_cfg slurm casa_container /idia/software/containers/casa-6.4.4-modular.simg)
THRESHOLD=$(read_cfg rmclean threshold 0.0000010)
ITERATIONS=$(read_cfg rmclean iterations 5000)
WINDOW=$(read_cfg rmclean window 0)
GAIN=$(read_cfg rmclean gain 0.1)

echo "RM container:   $RM_CONTAINER"
echo "CASA container: $CASA_CONTAINER"
echo ""

# Stage 1: Stokes Q/U extraction (only if full IQUV cube was supplied)
if [ -n "$FITS_FULL" ]; then
    echo "[Stage 1] Extracting Stokes Q/U from full cube..."
    singularity --quiet exec "$RM_CONTAINER" python3 ./create_subimage.py --inputcube "$FITS_FULL"
    BASENAME=$(basename "$FITS_FULL" .fits)
    FITS_Q="${{BASENAME}}.stokesQ.fits"
    FITS_U="${{BASENAME}}.stokesU.fits"
    echo "  -> Created: $FITS_Q"
    echo "  -> Created: $FITS_U"
else
    echo "[Stage 1] Skipping extraction (Q and U already provided)"
fi

# Stage 2: Generate the RM synthesis array sbatch
echo ""
echo "[Stage 2] Generating run_parallel_rmsy.sbatch..."
singularity --quiet exec "$RM_CONTAINER" python3 ./run_parallel_rmsy.py --parallel "$CHUNKS" \
    --inputFitsStokesQ "$FITS_Q" \
    --inputFitsStokesU "$FITS_U" \
    --freqList "$FREQLIST" \
    --rmsyCleanThrethold "$THRESHOLD" \
    --rmsyCleanIterations "$ITERATIONS" \
    --rmsyCleanWindow "$WINDOW" \
    --rmsyCleanGain "$GAIN" \
    --account "$ACCOUNT" \
    --casaContainer "$CASA_CONTAINER" \
    --rmContainer "$RM_CONTAINER" \
    --createSbatch

# Stage 3: Submit the RM synthesis array job (sub-submit from inside SLURM)
echo ""
echo "[Stage 3] Submitting RM synthesis array job..."
SLURMID_RMSY=$(sbatch run_parallel_rmsy.sbatch | awk '{{print $4}}')
echo "  -> RM synthesis array: SLURM job $SLURMID_RMSY"

# Stage 4: Generate merge sbatch, then sub-submit with dependency on Stage 3
echo ""
echo "[Stage 4] Generating merge_image_parts.sbatch..."
INPUT_CUBE="${{FITS_FULL:-$FITS_Q}}"
singularity --quiet exec "$RM_CONTAINER" python3 -c "
import os, sys
sys.path.insert(0, '.')
import merge_image_parts as m
class A:
    inputcube='$INPUT_CUBE'
    account='$ACCOUNT'
    rmContainer='$RM_CONTAINER'
    casaContainer='$CASA_CONTAINER'
m.write_sbatch_file(A.inputcube, account=A.account, rm_container=A.rmContainer, casa_container=A.casaContainer)
"

if [ -f merge_image_parts.sbatch ]; then
    SLURMID_MERGE=$(sbatch --dependency=afterok:$SLURMID_RMSY merge_image_parts.sbatch | awk '{{print $4}}')
    echo "  -> Merge: SLURM job $SLURMID_MERGE (depends on $SLURMID_RMSY)"
else
    echo "  WARNING: merge sbatch was not written; merge stage skipped."
    SLURMID_MERGE=""
fi

# Generate per-stage killJobs helpers (now that we know all the IDs)
write_kill () {{
    local script="$1"
    local ids="$2"
    local label="$3"
    cat > "$script" <<EOF
#!/bin/bash
# Kill the ${{label}} job(s).
# Generated by processRM at $(date -Iseconds)
set -e
IDS="$ids"
if [ -z "\$IDS" ]; then
    echo "No jobs to cancel for: ${{label}}"
    exit 0
fi
echo "Cancelling \$IDS (${{label}})"
scancel \$IDS
EOF
    chmod +x "$script"
}}

write_kill "killJobs_rmsynth_clean" "$SLURMID_RMSY" "RM synthesis + clean array (combined)"
write_kill "killJobs_merge"         "$SLURMID_MERGE" "merge"
write_kill "killJobs"               "$SLURM_JOB_ID $SLURMID_RMSY $SLURMID_MERGE" "all processRM stages"

echo ""
echo "===================================="
echo "  Pipeline submitted successfully!"
echo "===================================="
echo "Check status with:          ./fullSummary"
echo "Cancel everything:          ./killJobs"
echo "Cancel synth+clean array:   ./killJobs_rmsynth_clean"
echo "Cancel merge:               ./killJobs_merge"
"""

    # Templated values not safe to drop straight into f-string above:
    # they need to be substituted AFTER parsing the live config at generation time.
    # We read them from the config here (login-side) and bake into the orchestrator.
    taskvals, _ = config_parser.parse_config(config_path)
    account_val = (taskvals.get('slurm', {}).get('account') or 'b03-idia-ag').strip("'\"")
    rm_container_val = (taskvals.get('slurm', {}).get('rm_container') or '').strip("'\"")
    orchestrator = (orchestrator
                    .replace('__ACCOUNT__', account_val)
                    .replace('__RM_CONTAINER__', rm_container_val))

    with open(orch_path, 'w') as f:
        f.write(orchestrator)
    os.chmod(orch_path, 0o755)
    logger.info(f"Generated: {orch_path}")

    # ----- thin submit_pipeline.sh (calls sbatch only, runs anywhere) -----
    submit_wrapper = rf"""#!/bin/bash
# ==================================================================
#  processRM master submission script
# ==================================================================
# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
# Config: {config_path}
#
# This script ONLY calls sbatch (works from login, transfer, or any
# compute node). All real work runs inside SLURM jobs, on compute nodes,
# inside the rm-env container. Matches processMeerKAT's master design.

set -e
export PYTHONDONTWRITEBYTECODE=1   # keep __pycache__ out of the workdir
WORKDIR="{workdir}"
cd "$WORKDIR"

mkdir -p logs errors processing

echo "=================================================="
echo "  processRM Pipeline Submission"
echo "=================================================="
echo "Workdir: $WORKDIR"
echo ""
echo "Submitting orchestrator job to SLURM..."

JOBID=$(sbatch processRM_orchestrate.sbatch | awk '{{print $4}}')
echo "  -> Orchestrator: SLURM job $JOBID"

# killJobs_orchestrator - cancels only the master orchestrator job.
# Per-stage killJobs (killJobs_rmsynth, killJobs_merge, killJobs) are
# written by the orchestrator itself once it knows the sub-job IDs.
cat > killJobs_orchestrator <<EOF
#!/bin/bash
# Cancel only the processRM orchestrator job ($JOBID).
# Sub-jobs (RM synthesis array, merge) keep running unless you also
# run ./killJobs_rmsynth or ./killJobs_merge, or ./killJobs for all.
set -e
echo "Cancelling orchestrator $JOBID"
scancel $JOBID
EOF
chmod +x killJobs_orchestrator

echo ""
echo "It will:"
echo "  Stage 1 - Extract Stokes Q/U (if full IQUV cube provided)"
echo "  Stage 2 - Generate run_parallel_rmsy.sbatch"
echo "  Stage 3 - Sub-submit RM synthesis array (depends on Stage 1)"
echo "  Stage 4 - Generate merge sbatch + sub-submit (depends on Stage 3)"
echo ""
echo "Check status with:        ./fullSummary"
echo "Tail orchestrator:        tail -f logs/orchestrate-${{JOBID}}.out"
echo ""
echo "Cancel scripts (written now / by the orchestrator once it runs):"
echo "  ./killJobs_orchestrator    - cancel just this master job"
echo "  ./killJobs_rmsynth_clean   - cancel the RM synthesis + clean array (they share a job)"
echo "  ./killJobs_merge           - cancel the merge stage"
echo "  ./killJobs                 - cancel every processRM job here"
"""

    with open(submit_path, 'w') as f:
        f.write(submit_wrapper)
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

RUN_GENERATED_FILES = (
    'submit_pipeline.sh', 'processRM_orchestrate.sbatch',
    'run_parallel_rmsy.sbatch', 'merge_image_parts.sbatch',
    'config_parser.py', 'create_subimage.py', 'create_subimage_rmsy_cube.py',
    'run_parallel_rmsy.py', 'merge_image_parts.py', 'fullSummary',
    'killJobs', 'killJobs_orchestrator', 'killJobs_rmsynth_clean', 'killJobs_merge',
)

RUN_GENERATED_DIRS = (
    'logs', 'processing', 'errors', '__pycache__',
)


def cleanup_run_artifacts(workdir, keep_config=None):
    """Wipe everything in `workdir` that RUN mode could have produced so the
    user is left with a clean state (their config file only). Removes:
      - every symlink (RUN mode is the only thing that creates symlinks here)
      - every named generated file (sbatch, scripts, killJobs*)
      - every named generated directory (logs/, processing/, errors/, __pycache__/)
    `keep_config` (basename) is explicitly preserved.
    """
    removed = []

    # 1) named files
    for name in RUN_GENERATED_FILES:
        p = os.path.join(workdir, name)
        if os.path.islink(p) or os.path.exists(p):
            try:
                os.unlink(p)
                removed.append(name)
            except OSError:
                pass

    # 2) named directories (recursive)
    for name in RUN_GENERATED_DIRS:
        p = os.path.join(workdir, name)
        if os.path.isdir(p) and not os.path.islink(p):
            try:
                shutil.rmtree(p)
                removed.append(name + '/')
            except OSError:
                pass

    # 3) any leftover symlinks in the workdir (RUN's [data] links)
    #    plus stray CASA log files (e.g. casa-20260617-...log) that the
    #    extraction stage dumps into the workdir when it imports
    #    casatools/casatasks.
    import fnmatch
    try:
        for entry in os.listdir(workdir):
            if keep_config and entry == os.path.basename(keep_config):
                continue
            p = os.path.join(workdir, entry)
            if os.path.islink(p):
                try:
                    os.unlink(p)
                    removed.append(entry + ' (symlink)')
                except OSError:
                    pass
            elif fnmatch.fnmatch(entry.lower(), 'casa*.log'):
                try:
                    os.unlink(p)
                    removed.append(entry)
                except OSError:
                    pass
    except OSError:
        pass

    if removed:
        logger.warning(f"Cleaned up stale artifacts: {', '.join(removed)}")


def materialize_workdir_from_config(config_path, workdir):
    """RUN-mode setup: validate input files first, then symlink them into
    workdir, copy pipeline scripts in, and generate submit_pipeline.sh.

    If any input file is missing, we LOUDLY refuse to materialise and
    clean up any half-built scaffolding from a previous run so the
    user gets a fresh start once they fix the config.
    """
    taskvals, _ = config_parser.parse_config(config_path)
    data = taskvals.get('data', {})

    # ---- Pre-flight: validate every [data] path BEFORE touching anything ----
    missing = []
    for key in ('fits_full', 'fits_stokesQ', 'fits_stokesU', 'freqlist'):
        path = (data.get(key) or '').strip()
        if not path:
            continue
        if path == os.path.basename(path):
            # Config already has a basename — file must already exist in workdir
            dst = os.path.join(workdir, path)
            if not os.path.exists(dst):
                missing.append((key, path, dst))
        else:
            # Config has a directory part — verify the absolute source exists
            if not os.path.exists(path):
                missing.append((key, path, path))

    if missing:
        logger.error("=" * 60)
        logger.error("CANNOT MATERIALISE WORKDIR — input file(s) missing!")
        logger.error("=" * 60)
        for key, config_val, looked_for in missing:
            logger.error(f"  [data] {key} = '{config_val}'")
            logger.error(f"         tried to read: {looked_for}  (NOT FOUND)")
        logger.error("")
        logger.error("Fix the [data] paths in your config file, then re-run with -R.")
        logger.error(f"Or rebuild from scratch:")
        logger.error(f"   processRM -F /full/path/to/cube.fits -f /full/path/to/freqs.txt "
                     f"-C {os.path.basename(config_path)}")
        logger.error("")
        cleanup_run_artifacts(workdir, keep_config=config_path)
        logger.error("Run aborted. No SLURM jobs were submitted.")
        sys.exit(1)

    # ---- All paths good — materialise the workdir ----
    updates = {}
    for key in ('fits_full', 'fits_stokesQ', 'fits_stokesU', 'freqlist'):
        path = (data.get(key) or '').strip()
        if not path:
            continue
        if path == os.path.basename(path):
            # Already a local basename and file exists (verified above) — nothing to do
            continue
        basename = link_into_workdir(path, workdir, label=key)
        updates[('data', key)] = f"'{basename}'"

    if updates:
        update_config_inplace(config_path, updates)

    # Subdirectories — only now that we know we will actually run
    for sub in ['logs', 'processing', 'errors']:
        os.makedirs(os.path.join(workdir, sub), exist_ok=True)
    for sub in ['extract', 'rmsynth', 'rmclean', 'merge']:
        os.makedirs(os.path.join(workdir, 'errors', sub), exist_ok=True)

    copy_pipeline_scripts(workdir)
    shutil.copy2(os.path.join(SCRIPT_DIR, 'config_parser.py'),
                 os.path.join(workdir, 'config_parser.py'))
    return generate_submit_script(workdir, config_path)


def main():
    print(BANNER, flush=True)
    args = parse_args()

    # Ilifu environment check (processRM is ilifu-only)
    check_ilifu_environment()

    workdir = args.workdir or os.getcwd()
    workdir = os.path.abspath(workdir)
    if not os.path.exists(workdir):
        os.makedirs(workdir)

    print()
    logger.info(f"processRM v{__version__}")
    logger.info(f"Working directory: {workdir}")
    print()

    # Mode selection:
    #   -F BUILD mode: only writes/updates the config file
    #   -R RUN mode:   reads the config, creates symlinks, copies scripts,
    #                  generates submit_pipeline.sh, and (with -s) submits.
    # The two modes are mutually exclusive.
    if args.fitsfile and args.run_config:
        logger.error("Cannot use -F (build) and -R (run) at the same time.")
        sys.exit(1)

    if args.fitsfile:
        # BUILD: just write the config and stop.
        config_path = build_config_from_args(args, workdir)
        try:
            config_parser.validate_config(config_path)
            logger.info("Config validated successfully.")
        except (ValueError, KeyError, FileNotFoundError) as e:
            logger.error(f"Config validation failed: {e}")
            sys.exit(1)

        print("\n" + "=" * 50)
        print("  processRM config built!")
        print("=" * 50)
        print(f"  Workdir:    {workdir}")
        print(f"  Config:     {config_path}")
        print()
        print("  Review/edit the config, then run the pipeline:")
        print(f"     processRM -R {os.path.basename(config_path)}")
        print("=" * 50)
        print()
        return

    if not args.run_config:
        logger.error("Must provide either -F (build mode) or -R (run mode).")
        logger.error("Try 'processRM --help' for examples.")
        sys.exit(1)

    # RUN mode
    config_path = os.path.abspath(args.run_config)
    if not os.path.exists(config_path):
        logger.error(f"Config file not found: {config_path}")
        sys.exit(1)
    logger.info(f"Running with config: {config_path}")

    try:
        config_parser.validate_config(config_path)
        logger.info("Config validated successfully.")
    except (ValueError, KeyError, FileNotFoundError) as e:
        logger.error(f"Config validation failed: {e}")
        sys.exit(1)

    print()
    submit_script = materialize_workdir_from_config(config_path, workdir)

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
        print("  To submit the pipeline, run:")
        print("     ./submit_pipeline.sh")
        print()
        print("  To check status:")
        print("     ./fullSummary")
        print("=" * 50)
        print()


if __name__ == '__main__':
    main()
