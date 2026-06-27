#!/usr/bin/env python3
"""
==================================================================
   ____                              ____  __  __
  |  _ \\ _ __ ___   ___ ___  ___ ___|  _ \\|  \\/  |
  | |_) | '__/ _ \\ / __/ _ \\/ __/ __| |_) | |\\/| |
  |  __/| | | (_) | (_|  __/\\__ \\__ \\  _ <| |  | |
  |_|   |_|  \\___/ \\___\\___||___/___/_| \\_\\_||_|

  processRM - RM Synthesis Pipeline Orchestrator
  RM-synthesis made simple
==================================================================

Modeled after processMeerKAT.

Usage:
  BUILD a new config (you provide a full-Stokes IQUV cube + freq list):
    processRM -B -F mycube_IQUV.fits -f mycube.freqlist.txt
    processRM -B -F mycube_IQUV.fits -f freqs.txt -C run1.txt   # custom name

  RUN an existing config:
    processRM -R myconfig.txt
    processRM -R myconfig.txt -s          # also auto-submit

After build/setup, run:
    ./submit_pipeline.sh                  # submits SLURM jobs
    ./fullSummary                         # check pipeline status
"""

__version__ = '2.0'

import argparse
import os
import sys
import glob
import json
import shutil
import logging
import re
import subprocess
from datetime import datetime
from time import gmtime

import config_parser
import cube_validator
import region_parser


# ---- Container helpers ------------------------------------------------------
# All Python that needs astropy / RM-Tools runs inside the rm-env container;
# the helpers below run small snippets of cube_validator / region_parser via
# `singularity exec`. The local import paths exist only as a no-op fast path
# when astropy happens to be available on the host; logs don't mention that
# distinction because the container is the only intended source.


def _resolve_rm_container_path(args):
    """Resolve the rm-env.sif location BUILD should call into.

    Priority: --rm-container override > install default (~/processRM/container).
    The config-file value isn't consulted here because BUILD runs before the
    workdir config is written.
    """
    if getattr(args, 'rm_container_override', None):
        return os.path.abspath(os.path.expanduser(args.rm_container_override))
    return os.path.join(SCRIPT_DIR, 'container', 'rm-env.sif')


class _LoginNodeUnsupported(RuntimeError):
    """singularity exec itself can't run here and we have no usable fallback.
    On ilifu this typically means we're on the login node AND `sacctmgr`
    didn't return a default account for the srun wrapper."""


def _on_compute_node():
    """True when we're inside a SLURM allocation (srun/sbatch/small-sesh).
    Outside one, $SLURM_JOB_ID is unset -- that's the login node."""
    return bool(os.environ.get('SLURM_JOB_ID'))


_SLURM_DEFAULT_ACCOUNT_CACHE = []  # sentinel: empty = not resolved yet
_SLURM_VALID_ACCOUNTS_CACHE = []   # sentinel: empty = not resolved yet
_SLURM_ACCOUNT_WARNED = set()      # preferred accounts we've already warned about


def _slurm_default_account():
    """User's DefaultAccount from `sacctmgr`. Cached per process."""
    if _SLURM_DEFAULT_ACCOUNT_CACHE:
        return _SLURM_DEFAULT_ACCOUNT_CACHE[0]
    user = os.environ.get('USER', '')
    account = None
    if user:
        try:
            result = subprocess.run(
                ['sacctmgr', '-nP', 'show', 'user', user, 'format=DefaultAccount'],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                first = (result.stdout or '').strip().splitlines()
                if first and first[0].strip():
                    account = first[0].strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    _SLURM_DEFAULT_ACCOUNT_CACHE.append(account)
    return account


def _slurm_valid_accounts():
    """Every SLURM account this user can submit under, from `sacctmgr`.
    Returns [] if sacctmgr is unavailable (in which case we can't validate
    the user's choice and silently accept it)."""
    if _SLURM_VALID_ACCOUNTS_CACHE:
        return _SLURM_VALID_ACCOUNTS_CACHE[0]
    user = os.environ.get('USER', '')
    accounts = []
    if user:
        try:
            result = subprocess.run(
                ['sacctmgr', '-nP', 'show', 'association',
                 f'user={user}', 'format=Account'],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                seen = set()
                for line in (result.stdout or '').splitlines():
                    acct = line.strip()
                    if acct and acct not in seen:
                        seen.add(acct)
                        accounts.append(acct)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    _SLURM_VALID_ACCOUNTS_CACHE.append(accounts)
    return accounts


def _resolve_slurm_account(preferred=None):
    """Pick the SLURM account to use for an srun-wrapped container exec.

    Priority:
      1. `preferred` (typically the config's [slurm] account) -- used as-is
         when sacctmgr can't enumerate the user's accounts (we trust the
         user) or when it's in the user's valid list.
      2. If `preferred` is given but is NOT in the user's valid list, warn
         (once per preferred value) and fall back to the user's
         DefaultAccount.
      3. If `preferred` is empty/None, return the DefaultAccount.

    Returns None if no account can be determined at all.
    """
    preferred = (preferred or '').strip().strip("'\"") or None
    if preferred is None:
        return _slurm_default_account()
    valid = _slurm_valid_accounts()
    if not valid or preferred in valid:
        return preferred
    if preferred not in _SLURM_ACCOUNT_WARNED:
        _SLURM_ACCOUNT_WARNED.add(preferred)
        default = _slurm_default_account()
        logger.warning(
            f"  -> SLURM account '{preferred}' from the config is NOT in "
            f"your list of valid accounts ({', '.join(valid)}). "
            f"Falling back to your default account "
            f"({default if default else 'unknown'}). Edit [slurm] account "
            f"in the config to one of the valid accounts to silence this."
        )
    return _slurm_default_account()


def _container_python(container_path, code, timeout=180, account=None):
    """Run a Python snippet inside the rm-env container; return stdout (str).

    On a compute node (``$SLURM_JOB_ID`` is set) we call singularity exec
    directly. On a login node we wrap it in ``srun`` so the snippet runs
    on a compute node and stdout streams back to the caller's terminal --
    ilifu login nodes don't grant user-namespace mappings, so a direct
    singularity exec there would always fail.

    ``account`` (optional) is the preferred SLURM account from the config;
    if the user doesn't have access to it we fall back to their default
    (and warn once). Pass None to always use the default.

    Raises:
      FileNotFoundError       -- container_path doesn't exist, or singularity
                                 / srun binary missing.
      _LoginNodeUnsupported   -- we're on a login node but can't determine a
                                 SLURM account for the srun wrapper.
      RuntimeError            -- non-zero exit from the snippet itself.
    """
    if not os.path.exists(container_path):
        raise FileNotFoundError(container_path)

    if _on_compute_node():
        cmd = ['singularity', '--quiet', 'exec', container_path,
               'python3', '-c', code]
        outer_timeout = timeout
    else:
        # Login node -> srun a tiny compute slot. Defaults match
        # processMeerKAT's lightweight validation jobs: 1 CPU, 2 GB, 5 min.
        account = _resolve_slurm_account(preferred=account)
        if account is None:
            raise _LoginNodeUnsupported(
                "BUILD-time validation needs to run on a compute node but "
                "couldn't determine your SLURM account (sacctmgr did not "
                "return a DefaultAccount). Set one with: "
                "`sacctmgr modify user name=$USER set DefaultAccount=<account>` "
                "or grab a compute session manually (e.g. `small-sesh`)."
            )
        logger.info(f"  -> running validation on a compute node via srun "
                    f"(account={account}, this can take a few seconds)...")
        cmd = ['srun', '--quiet',
               '--partition=Main',
               '--time=00:05:00',
               '--mem=2G',
               '--cpus-per-task=1',
               f'--account={account}',
               'singularity', '--quiet', 'exec', container_path,
               'python3', '-c', code]
        # Generous wall-time -- queue wait + exec time.
        outer_timeout = timeout + 600

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=outer_timeout,
        )
    except FileNotFoundError as e:
        raise FileNotFoundError(str(e))

    if result.returncode != 0:
        err = (result.stderr or result.stdout or 'exec failed').strip()
        # If srun managed to land us on a node that ALSO can't unshare
        # namespaces, or if we're on a compute node misconfigured the same
        # way, surface the cleaner message instead of the raw Apptainer dump.
        if 'setgroups' in err or 'user namespace mappings' in err:
            raise _LoginNodeUnsupported(
                "singularity exec couldn't unshare a user namespace even on "
                "the compute node it ran on. Try a different partition or "
                "ask ilifu support."
            )
        raise RuntimeError(err)
    return result.stdout


def _validate_cube_via_container(container_path, cube_path, freqlist_path, account=None):
    """Re-run cube_validator.validate_freqlist_against_cube inside the container.

    ``account`` (optional): preferred SLURM account from the config; falls
    back to user's default if not in the user's valid-accounts list.

    Returns the same dict structure the local helper does. Raises
    ``cube_validator.CubeStructureError`` if the validation failed inside the
    container (a real structural problem with the cube, not an env issue).
    """
    code = (
        "import sys, json\n"
        f"sys.path.insert(0, {SCRIPT_DIR!r})\n"
        "import cube_validator\n"
        "try:\n"
        f"    info = cube_validator.validate_freqlist_against_cube({cube_path!r}, {freqlist_path!r})\n"
        # the axes dict is fine; ints are JSON-safe
        "    print(json.dumps({'ok': True, 'info': info}))\n"
        "except cube_validator.CubeStructureError as e:\n"
        "    print(json.dumps({'ok': False, 'error': str(e)}))\n"
    )
    stdout = _container_python(container_path, code, account=account)
    payload = json.loads(stdout.strip().splitlines()[-1])
    if not payload['ok']:
        raise cube_validator.CubeStructureError(payload['error'])
    return payload['info']


def _has_beam_info_via_container(container_path, cube_path, account=None):
    """Re-run cube_validator.has_beam_info inside the container."""
    code = (
        "import sys, json\n"
        f"sys.path.insert(0, {SCRIPT_DIR!r})\n"
        "import cube_validator\n"
        f"print(json.dumps(cube_validator.has_beam_info({cube_path!r})))\n"
    )
    stdout = _container_python(container_path, code, account=account)
    return json.loads(stdout.strip().splitlines()[-1])


def _naxis2_via_container(container_path, cube_path, account=None):
    """Return NAXIS2 (image height in pixels) read inside the container."""
    code = (
        "import json\n"
        "from astropy.io import fits\n"
        f"print(json.dumps(int(fits.getheader({cube_path!r})['NAXIS2'])))\n"
    )
    stdout = _container_python(container_path, code, account=account)
    return json.loads(stdout.strip().splitlines()[-1])


def _transpose_cube_via_container(container_path, cube_path, account=None):
    """Run cube_validator.transpose_cube_in_place inside the container.

    Returns True if a transpose actually happened, False otherwise.
    """
    code = (
        "import sys, json\n"
        f"sys.path.insert(0, {SCRIPT_DIR!r})\n"
        "import cube_validator\n"
        f"changed = cube_validator.transpose_cube_in_place({cube_path!r})\n"
        "print(json.dumps(bool(changed)))\n"
    )
    stdout = _container_python(container_path, code, timeout=600, account=account)
    return json.loads(stdout.strip().splitlines()[-1])


def _parse_region_via_container(container_path, region_path, cube_path, account=None):
    """Re-run region_parser.parse_region_file inside the container.

    Loads the cube's WCS header inside the container so world-coord regions
    can be converted to pixel coords. Returns the same list of dicts the
    local helper does.
    """
    code = (
        "import sys, json\n"
        f"sys.path.insert(0, {SCRIPT_DIR!r})\n"
        "from astropy.io import fits\n"
        "import region_parser\n"
        f"hdr = fits.getheader({cube_path!r})\n"
        f"regs = region_parser.parse_region_file({region_path!r}, hdr)\n"
        "print(json.dumps(regs))\n"
    )
    stdout = _container_python(container_path, code, account=account)
    return json.loads(stdout.strip().splitlines()[-1])


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
  # BUILD a config from a full Stokes IQUV cube + freq list (default name: myconfig.txt)
  processRM -B -F mycube_IQUV.fits -f mycube.freqlist.txt

  # BUILD with a custom config name
  processRM -B -F mycube_IQUV.fits -f mycube.freqlist.txt -C run1.txt

  # BUILD with a specific chunk count
  processRM -B -F mycube_IQUV.fits -f freqs.txt --chunks 50

  # BUILD and submit immediately
  processRM -B -F mycube_IQUV.fits -f freqs.txt -s

  # RUN an existing config (regenerate submit_pipeline.sh from it)
  processRM -R myconfig.txt

  # RUN and submit immediately
  processRM -R myconfig.txt -s
"""
    )

    parser.add_argument('-B', '--build', action='store_true',
                        help='BUILD mode: generate a new config from the inputs given via '
                             '-F / -f / -r / --chunks. Does not submit anything unless -s is '
                             'also passed. Mutually exclusive with -R.')
    parser.add_argument('-F', '--fitsfile',
                        help='BUILD mode: path to the full-Stokes IQUV radio-continuum cube '
                             '(e.g. mycube_IQUV.fits). processRM extracts Stokes I, Q, and U '
                             'internally; standalone Q/U cubes are not accepted. Relative or '
                             'absolute paths are fine — processRM will symlink the file into '
                             'the current directory so all outputs land here.')
    parser.add_argument('-f', '--freqlist',
                        help='Path to frequency list (.txt). Required when -B is used.')
    parser.add_argument('-r', '--region-file', dest='region_file', default=None,
                        help='Optional CARTA region file (.crtf or .ds9, pixel or world). '
                             'Boxed regions only. Each box becomes an independent processing '
                             'branch. When set, the config [data] crop/pointing values are ignored.')
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

    # -F must be a single full-Stokes IQUV cube. Q/U-only inputs are not accepted.
    fits_files = args.fitsfile.split()
    if len(fits_files) != 1:
        logger.error(
            "-F expects exactly one path: a full-Stokes IQUV radio-continuum cube. "
            f"Got {len(fits_files)} entries. Q/U-only inputs are no longer supported."
        )
        sys.exit(1)
    fits_full = os.path.abspath(fits_files[0])
    if not os.path.exists(fits_full):
        logger.error(f"FITS file not found: {fits_full}")
        sys.exit(1)
    validate_ilifu_path(fits_full, label="fits_full")

    freqlist = os.path.abspath(args.freqlist)

    # ---- Cube structure + freqlist validation (BUILD-time) ----
    # Runs inside the rm-env container (where astropy lives). If the container
    # isn't built yet we defer to RUN, which uses the same container.
    primary_cube = fits_full
    rm_container_path = _resolve_rm_container_path(args)
    cube_info = None
    try:
        cube_info = cube_validator.validate_freqlist_against_cube(primary_cube, freqlist)
    except cube_validator.CubeStructureError as e:
        msg = str(e)
        if 'astropy is required' in msg:
            try:
                cube_info = _validate_cube_via_container(
                    rm_container_path, primary_cube, freqlist
                )
            except FileNotFoundError as fe:
                logger.warning(f"  -> skipping BUILD-time cube validation: container "
                               f"not found at {fe}. Run setup.sh to fetch it. "
                               f"RUN will validate.")
            except _LoginNodeUnsupported as e:
                logger.warning(f"  -> skipping BUILD-time cube validation: {e}")
            except RuntimeError as re_err:
                logger.warning(f"  -> cube validation failed inside the container: "
                               f"{re_err}. RUN will retry.")
            except cube_validator.CubeStructureError as ce:
                logger.error(str(ce))
                sys.exit(1)
        else:
            logger.error(msg)
            sys.exit(1)
    if cube_info:
        logger.info(
            f"cube structure OK: NAXIS={cube_info['naxis']}, "
            f"FREQ on axis {cube_info['freq_axis']} ({cube_info['freq_nchans']} chans), "
            f"STOKES on axis {cube_info['stokes_axis']}, freqlist lines={cube_info['freqlist_lines']}"
        )
        if cube_info['needs_transpose']:
            logger.warning(
                "  -> axes are (RA, DEC, STOKES, FREQ). RUN mode will auto-transpose "
                "the (symlinked) cube to (RA, DEC, FREQ, STOKES) before chunking. "
                "The original file on /idia/ or /users/ is NOT modified."
            )

    # ---- Region file: parse + preview if provided ----
    # World-coord regions need astropy's WCS to convert RA/Dec to pixels, so
    # the parse runs inside the container.
    region_file_abs = ''
    if args.region_file:
        region_file_abs = os.path.abspath(args.region_file)
        if not os.path.exists(region_file_abs):
            logger.error(f"Region file not found: {region_file_abs}")
            sys.exit(1)
        validate_ilifu_path(region_file_abs, label="region_file")
        wcs_header = None
        try:
            from astropy.io import fits as _fits
            wcs_header = _fits.getheader(primary_cube)
        except Exception:
            pass
        regions = None
        try:
            regions = region_parser.parse_region_file(region_file_abs, wcs_header)
        except region_parser.RegionParseError as e:
            if 'no cube WCS was supplied' in str(e):
                try:
                    regions = _parse_region_via_container(
                        rm_container_path, region_file_abs, primary_cube
                    )
                except FileNotFoundError as fe:
                    logger.warning(f"  -> skipping BUILD-time region preview: container "
                                   f"not found at {fe}. RUN will parse.")
                except _LoginNodeUnsupported as e:
                    logger.warning(f"  -> skipping BUILD-time region preview: {e}")
                except RuntimeError as re_err:
                    logger.warning(f"  -> region parsing failed inside the container: "
                                   f"{re_err}. RUN will retry.")
            else:
                logger.error(f"Region file '{region_file_abs}': {e}")
                sys.exit(1)
        if regions:
            logger.info(f"parsed {len(regions)} region(s) from "
                        f"{os.path.basename(region_file_abs)}:")
            for line in region_parser.describe_regions(regions):
                logger.info(f"  -> {line}")
        logger.warning(
            "  -> region_file overrides [data] crop and pointing in the config."
        )

    # Copy default config to workdir under user-chosen name (or 'myconfig.txt')
    config_name = args.config if args.config else 'myconfig.txt'
    if not config_name.endswith('.txt'):
        config_name += '.txt'
    config_path = os.path.join(workdir, os.path.basename(config_name))
    if os.path.exists(config_path):
        # Move the existing config to <name>.bck (overwriting any older
        # backup) so a fresh BUILD never silently loses the user's last
        # set of edits. Restore by `mv myconfig.txt.bck myconfig.txt`.
        backup_path = config_path + '.bck'
        shutil.move(config_path, backup_path)
        logger.warning(f"Existing config backed up to: {backup_path}")
    shutil.copy2(DEFAULT_CONFIG, config_path)
    logger.info(f"Created config file: {config_path}")

    # Use the parsed default config to determine the rm_container fallback,
    # then write all updates back via the format-preserving inline updater so
    # the per-parameter comments in default_config.txt survive.
    taskvals, _ = config_parser.parse_config(config_path)

    updates = {
        ('data', 'fits_full'):     f"'{fits_full}'",
        ('data', 'freqlist'):      f"'{freqlist}'",
        ('data', 'region_file'):   f"'{region_file_abs}'",
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

    # Beam-info heads-up (the config now exists, so the user can act on the
    # advice by editing it before running -R). RUN re-checks and refuses to
    # submit if this is still in a bad state.
    _check_beam_for_sigma(primary_cube, taskvals, config_path, hard=False,
                          rm_container_path=rm_container_path)

    # [CHANGE 2026-06-16]: Geometry preview
    # The chunker uses int(NAXIS2 / parallel) which truncates, and the last chunk
    # absorbs the remainder. Print that math up front so the user can sanity-check
    # before submitting a 100-task array.
    _preview_chunk_geometry(args, fits_full, workdir, taskvals)

    return config_path


def _check_beam_for_sigma(cube_path, taskvals, config_path, hard,
                          rm_container_path=None, account=None):
    """Verify that the cube has the beam metadata PyBDSF needs to build a
    noise map, but ONLY when the config asks for sigma cleaning AND no
    pre-computed noise map is supplied.

    Reads the live [rmclean] threshold and [noise] noise_map from ``taskvals``
    so the check reflects whatever the user has (or hasn't) edited in the
    config. Returns True if it's safe to proceed.

    ``hard=False`` (BUILD): on failure, log a WARN block and return False.
    The config has just been created, so the user can act on the advice
    by editing it before they run ``-R``.
    ``hard=True`` (RUN): on failure, log an ERROR block and ``sys.exit(1)``.
    We're about to submit SLURM jobs that would crash inside the noise stage.
    """
    try:
        cfg_threshold = float(taskvals.get('rmclean', {}).get('threshold', -5))
    except (TypeError, ValueError):
        cfg_threshold = -5.0
    cfg_noise_map = (taskvals.get('noise', {}).get('noise_map') or '').strip()

    if cfg_threshold >= 0 or cfg_noise_map:
        return True  # absolute mode, or user supplied their own noise map -> no PyBDSF needed

    try:
        beam = cube_validator.has_beam_info(cube_path)
    except ImportError:
        # Beam check runs inside the container (where astropy lives).
        if rm_container_path is None:
            beam = None
        else:
            try:
                beam = _has_beam_info_via_container(rm_container_path, cube_path,
                                                    account=account)
            except _LoginNodeUnsupported as e:
                if not hard:
                    logger.warning(f"  -> skipping BUILD-time beam-info check: {e}")
                    return True
                raise
            except (FileNotFoundError, RuntimeError) as e:
                if not hard:
                    logger.warning(f"  -> skipping BUILD-time beam-info check: "
                                   f"container check failed ({e}). RUN will "
                                   f"validate before submitting any jobs.")
                    return True
                raise
    if beam is not None:
        logger.info(f"beam info OK ({beam}); sigma cleaning will use a PyBDSF noise map.")
        return True

    log = logger.error if hard else logger.warning
    title = ("REFUSING TO RUN: sigma cleaning requested but cube has no beam metadata"
             if hard else
             "Heads-up: the default sigma cleaning won't work for this cube")
    log("=" * 60)
    log(title)
    log("=" * 60)
    log(f"  cube:      {cube_path}")
    log(f"  threshold: {cfg_threshold}  (negative => N-sigma per pixel)")
    log("")
    log("PyBDSF (used to build the noise map) needs either:")
    log("  - BMAJ/BMIN/BPA in the primary FITS header, or")
    log("  - a CASA-style BEAMS table HDU (per-channel beam parameters)")
    log("Neither was found in your cube.")
    log("")
    log(f"Edit {config_path}, pick one, then continue:")
    log("  1. Switch to an absolute cutoff:")
    log("       [rmclean] threshold = 0.0000010   # positive = Jy/beam/RMSF")
    log("  2. Pre-compute a 2D noise map yourself and point at it:")
    log("       [noise]   noise_map = '/path/to/noise_map.fits'")
    log("  3. Re-image the cube so it retains beam metadata, then re-build.")
    if hard:
        log("")
        sys.exit(1)
    return False


def _preview_chunk_geometry(args, fits_full, workdir, taskvals):
    """Read NAXIS2 from the input cube and log how it will be split.

    Runs the read inside the rm-env container so the preview works on the
    login node too.
    """
    if not fits_full or not os.path.exists(fits_full):
        return
    naxis2 = None
    try:
        from astropy.io import fits
        naxis2 = int(fits.getheader(fits_full)['NAXIS2'])
    except Exception:
        try:
            naxis2 = _naxis2_via_container(_resolve_rm_container_path(args), fits_full)
        except (FileNotFoundError, RuntimeError):
            return  # silently skip if neither path works -- chunk preview is non-essential

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


_SINGLE_CUBE_BODY = r"""
# ---- SINGLE-CUBE FLOW (no region file in config) ----

BASENAME=$(basename "$FITS_FULL" .fits)
FITS_Q="${BASENAME}.stokesQ.fits"
FITS_U="${BASENAME}.stokesU.fits"
FITS_I="${BASENAME}.stokesI.fits"

# Stage 1: Extract Stokes I, Q, U from the full IQUV cube (gated by [run] stages='extract')
if has_stage extract; then
    if [ "$RESUME" = "True" ] && [ -f "$FITS_Q" ] && [ -f "$FITS_U" ] && [ -f "$FITS_I" ]; then
        echo "[Stage 1] SKIP — Stokes Q/U/I already extracted."
    else
        echo "[Stage 1] Extracting Stokes I/Q/U from full IQUV cube..."
        singularity --quiet exec "$RM_CONTAINER" python3 ./create_subimage.py --inputcube "$FITS_FULL"
        echo "  -> Created: $FITS_Q"
        echo "  -> Created: $FITS_U"
        echo "  -> Created: $FITS_I"
    fi
else
    echo "[Stage 1] Skipping extract ([run] stages=$STAGES)"
fi

# Stage 1.5: Per-pixel noise map (PyBDSF on Stokes I).
# Skip when [rmclean] threshold is positive (absolute mode) or when the user
# supplied a pre-computed noise map in [noise] noise_map.
# Also gated by 'extract' stage membership (the noise map is part of the
# input-preparation stage from the pipeline's perspective).
NOISE_MAP="noise_map.fits"
SLURMID_NOISE=""
if ! has_stage extract; then
    echo "[Stage 1.5] Skipping noise map ([run] stages=$STAGES)"
elif [ -n "$NOISE_MAP_CFG" ]; then
    echo "[Stage 1.5] Using pre-supplied noise map from config: $NOISE_MAP_CFG"
    NOISE_MAP="$NOISE_MAP_CFG"
elif awk "BEGIN{exit !($THRESHOLD < 0)}"; then
    if [ "$RESUME" = "True" ] && [ -f "$NOISE_MAP" ]; then
        # Re-run: noise map from a previous submission is still on disk.
        echo "[Stage 1.5] $NOISE_MAP already exists -- skipping noise generation."
    else
        echo "[Stage 1.5] Submitting make_noise.sbatch (sigma mode, threshold=$THRESHOLD)..."
        cat > make_noise.sbatch <<NOISEEOF
#!/bin/bash
#SBATCH --nodes=${NODES}
#SBATCH --ntasks-per-node=${NTASKS_PER_NODE}
#SBATCH --cpus-per-task=${CPUS_PER_TASK}
#SBATCH --mem=${MEM_NOISE}GB
#SBATCH --job-name=noise
#SBATCH --output=logs/noise-%j.out
#SBATCH --error=logs/noise-%j.err
#SBATCH --partition=${PARTITION}
#SBATCH --time=${TIME}
#SBATCH --account=$ACCOUNT

set -e
export PYTHONDONTWRITEBYTECODE=1
cd "$WORKDIR"
singularity --quiet exec "$RM_CONTAINER" python -m RMtools_3D.make_noise_map \
    "$FITS_Q" "$FITS_U" -o "$NOISE_MAP" -B "$FITS_FULL" -v
NOISEEOF
        SLURMID_NOISE=$(sbatch make_noise.sbatch | awk '{print $4}')
        echo "  -> Noise-map job: SLURM $SLURMID_NOISE"
    fi
else
    echo "[Stage 1.5] Absolute-threshold mode (threshold=$THRESHOLD); skipping noise stage."
    NOISE_MAP=""
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
    --phimax "$PHIMAX" \
    --dphi "$DPHI" \
    --nsamples "$NSAMPLES" \
    --weightType "$WEIGHTTYPE" \
    --fitGaussianRmsf "$FIT_GAUSSIAN_RMSF" \
    --superResolution "$SUPER_RESOLUTION" \
    --skipRmsf "$SKIP_RMSF" \
    --account "$ACCOUNT" \
    --casaContainer "$CASA_CONTAINER" \
    --rmContainer "$RM_CONTAINER" \
    --noiseMap "$NOISE_MAP" \
    --mem "$MEM" \
    --ncores "$NCORES" \
    --internalChunk "$INTERNAL_CHUNK" \
    --partition "$PARTITION" \
    --time "$TIME" \
    --nodes "$NODES" \
    --ntasksPerNode "$NTASKS_PER_NODE" \
    --cpusPerTask "$CPUS_PER_TASK" \
    --stages "$STAGES" \
    --resume "$RESUME" \
    --createSbatch

# Stage 3: Submit the RM synthesis array job (depends on noise stage if any).
# Resume-aware: pre-scan processing/ for already-complete chunks
# (FDF_clean_tot.fits present) and submit only the MISSING task IDs via
# `sbatch --array=<missing>`. That way a re-run with one OOMed chunk only
# asks SLURM for one mem_chunk allocation instead of all CHUNKS.
echo ""
SLURMID_RMSY=""
DEP_RMSY=""
# --kill-on-invalid-dep=yes so SLURM auto-cancels rmsy if the noise job
# fails, instead of leaving it pending with DependencyNeverSatisfied.
[ -n "$SLURMID_NOISE" ] && DEP_RMSY="--dependency=afterok:$SLURMID_NOISE --kill-on-invalid-dep=yes"
if ! has_stage rmsynth && ! has_stage rmclean; then
    echo "[Stage 3] Skipping RM synthesis array ([run] stages=$STAGES)"
elif [ "$RESUME" = "True" ]; then
    echo "[Stage 3] Pre-scanning processing/ for already-complete chunks..."
    MISSING=""
    for i in $(seq 1 $CHUNKS); do
        [ -f "processing/part_${i}_FDF_clean_tot.fits" ] || MISSING="${MISSING}${i},"
    done
    MISSING="${MISSING%,}"
    NUM_MISSING=$([ -z "$MISSING" ] && echo 0 || echo "$MISSING" | tr ',' '\n' | wc -l)
    if [ "$NUM_MISSING" = "0" ]; then
        echo "[Stage 3] All $CHUNKS chunks already complete -- skipping rmsy submission."
    elif [ "$NUM_MISSING" = "$CHUNKS" ]; then
        echo "[Stage 3] All $CHUNKS chunks need processing; submitting full array..."
        SLURMID_RMSY=$(sbatch $DEP_RMSY run_parallel_rmsy.sbatch | awk '{print $4}')
    else
        echo "[Stage 3] $NUM_MISSING of $CHUNKS chunks missing; submitting trimmed array..."
        SLURMID_RMSY=$(sbatch $DEP_RMSY --array="$MISSING" run_parallel_rmsy.sbatch | awk '{print $4}')
    fi
else
    echo "[Stage 3] RESUME=False -> submitting full array (no pre-scan)..."
    SLURMID_RMSY=$(sbatch $DEP_RMSY run_parallel_rmsy.sbatch | awk '{print $4}')
fi
echo "  -> RM synthesis array: SLURM job ${SLURMID_RMSY:-(none submitted)} ${DEP_RMSY:+(waits on $SLURMID_NOISE)}"

# Stage 4: Write a merge-prep SLURM job that depends on Stage 3 finishing.
echo ""
echo "[Stage 4] Writing merge-prep sbatch (will run after $SLURMID_RMSY)..."
INPUT_CUBE="$FITS_FULL"
cat > merge_prep.sbatch <<MPEOF
#!/bin/bash
#SBATCH --nodes=${NODES}
#SBATCH --ntasks-per-node=${NTASKS_PER_NODE}
#SBATCH --cpus-per-task=${CPUS_PER_TASK}
#SBATCH --mem=4GB
#SBATCH --job-name=merge_prep
#SBATCH --output=logs/merge_prep-%j.out
#SBATCH --error=logs/merge_prep-%j.err
#SBATCH --partition=${PARTITION}
#SBATCH --time=00:30:00
#SBATCH --account=$ACCOUNT

set -e
export PYTHONDONTWRITEBYTECODE=1
cd "$WORKDIR"

echo "[merge_prep] Generating merge_image_parts.sbatch from populated processing/..."
singularity --quiet exec "$RM_CONTAINER" python3 -c "
import sys
sys.path.insert(0, '.')
import merge_image_parts as m
m.write_sbatch_file('$INPUT_CUBE',
                    account='$ACCOUNT',
                    rm_container='$RM_CONTAINER',
                    casa_container='$CASA_CONTAINER',
                    mem='$MEM_MERGE',
                    time='$TIME_MERGE',
                    partition='$PARTITION',
                    nodes='$NODES',
                    ntasks_per_node='$NTASKS_PER_NODE',
                    cpus_per_task='$CPUS_PER_TASK',
                    run_fits2idia='$RUN_FITS2IDIA',
                    fix_invalid_stokes='$FIX_INVALID_STOKES')
"

if [ -f merge_image_parts.sbatch ]; then
    SLURMID_MERGE=\$(sbatch merge_image_parts.sbatch | awk '{print \$4}')
    echo "[merge_prep] Submitted merge array: \$SLURMID_MERGE"
    cat > killJobs_merge <<EOF2
#!/bin/bash
echo "Cancelling \$SLURMID_MERGE (merge array)"
scancel \$SLURMID_MERGE
EOF2
    chmod +x killJobs_merge

    # Generate + submit a tiny finalize job that removes orphaned CASA log
    # files (casa-*.log) from the workdir once every merge task has run.
    # afterany so it still cleans up even if some merges failed.
    cat > finalize.sbatch <<FINEOF
#!/bin/bash
#SBATCH --nodes=${NODES}
#SBATCH --ntasks-per-node=${NTASKS_PER_NODE}
#SBATCH --cpus-per-task=${CPUS_PER_TASK}
#SBATCH --mem=1GB
#SBATCH --job-name=finalize
#SBATCH --output=logs/finalize-%j.out
#SBATCH --error=logs/finalize-%j.err
#SBATCH --partition=${PARTITION}
#SBATCH --time=00:05:00
#SBATCH --account=$ACCOUNT

set -e
cd "$WORKDIR"
echo "[finalize] removing casa-*.log files from \$(pwd)..."
rm -fv casa-*.log 2>/dev/null || true
echo "[finalize] done."
FINEOF
    SLURMID_FINALIZE=\$(sbatch --dependency=afterany:\$SLURMID_MERGE finalize.sbatch | awk '{print \$4}')
    echo "[merge_prep] Submitted finalize: \$SLURMID_FINALIZE (cleans casa logs after merge)"
else
    echo "[merge_prep] WARNING: merge_image_parts.sbatch was not written."
fi
MPEOF
# Gate merge submission on [merge] run_merge AND [run] stages containing 'merge'.
SLURMID_MERGEPREP=""
SLURMID_MERGE=""
if [ "$RUN_MERGE" = "True" ] && has_stage merge; then
    # Only condition merge_prep on rmsy if rmsy was actually submitted; if every
    # chunk was already complete (SLURMID_RMSY empty), run merge_prep immediately.
    DEP_MERGE=""
    [ -n "$SLURMID_RMSY" ] && DEP_MERGE="--dependency=afterok:$SLURMID_RMSY --kill-on-invalid-dep=yes"
    SLURMID_MERGEPREP=$(sbatch $DEP_MERGE merge_prep.sbatch | awk '{print $4}')
    echo "  -> Merge prep: SLURM job $SLURMID_MERGEPREP ${DEP_MERGE:+(depends on $SLURMID_RMSY)}"
    SLURMID_MERGE="$SLURMID_MERGEPREP"
else
    echo "  -> Skipping merge stage ([merge] run_merge=$RUN_MERGE, [run] stages=$STAGES)"
fi

write_kill () {
    local script="$1"; local ids="$2"; local label="$3"
    cat > "$script" <<EOF
#!/bin/bash
set -e
IDS="$ids"
[ -z "\$IDS" ] && { echo "No jobs to cancel for: ${label}"; exit 0; }
echo "Cancelling \$IDS (${label})"
scancel \$IDS
EOF
    chmod +x "$script"
}
write_kill "killJobs_noise"         "$SLURMID_NOISE" "noise-map (PyBDSF)"
write_kill "killJobs_rmsynth_clean" "$SLURMID_RMSY" "RM synthesis + clean array"
write_kill "killJobs_merge"         "$SLURMID_MERGE" "merge"
write_kill "killJobs"               "$SLURM_JOB_ID $SLURMID_NOISE $SLURMID_RMSY $SLURMID_MERGE" "all processRM stages"

echo ""
echo "===================================="
echo "  Pipeline submitted successfully!"
echo "===================================="
"""


def _multi_region_body(regions):
    """Render the orchestrator body that loops over each region.

    Each region gets its own ``region<N>/`` subdir under WORKDIR. Inside it
    we symlink the cube, freqlist, and pipeline scripts, then run the same
    extract -> sbatch-gen -> sub-submit sequence as the single-cube flow,
    cd-ed into the region dir so all outputs land per-region.

    Region geometry is baked in as parallel bash arrays so the orchestrator
    has no runtime parse dependency on the region file.
    """
    ids = ' '.join(str(r['index']) for r in regions)
    # crop format that create_subimage.py / get_cropped_numpy_plane expect: [width, height]
    crops = ' '.join(f'"[{r["width_px"]},{r["height_px"]}]"' for r in regions)
    # pointing format: [x_center, y_center]
    points = ' '.join(
        f'"[{int(round(r["x_center_px"]))},{int(round(r["y_center_px"]))}]"'
        for r in regions
    )
    # Optional per-region labels lifted from the region file (label="..." in
    # CRTF, text={...} in DS9). _safe_label in region_parser strips quotes
    # and backslashes so these are safe to drop straight into a bash array.
    labels = ' '.join(f'"{(r.get("label") or "")}"' for r in regions)
    return r"""
# ---- MULTI-REGION FLOW (region_file in config -> one branch per box) ----

REGION_IDS=(""" + ids + r""")
REGION_CROPS=(""" + crops + r""")
REGION_POINTS=(""" + points + r""")
REGION_LABELS=(""" + labels + r""")

ALL_NOISE_IDS=""
ALL_RMSY_IDS=""
ALL_MERGEPREP_IDS=""

for i in "${!REGION_IDS[@]}"; do
    RID=${REGION_IDS[$i]}
    CROP=${REGION_CROPS[$i]}
    POINT=${REGION_POINTS[$i]}
    LABEL=${REGION_LABELS[$i]}
    SUFFIX="_r${RID}"
    REGION_DIR="region${RID}"

    echo ""
    echo "=========================================="
    echo "  Region ${RID}${LABEL:+ ($LABEL)}: crop=${CROP} pointing=${POINT}"
    echo "=========================================="
    mkdir -p "$REGION_DIR"
    cd "$REGION_DIR"
    mkdir -p logs errors processing
    # Persist the label so fullSummary can render it next to "Region N".
    [ -n "$LABEL" ] && echo "$LABEL" > region.label

    # Symlink everything the per-region scripts need (cube, freqlist, support code)
    for f in "$FITS_FULL" "$FREQLIST"; do
        [ -n "$f" ] && [ -f "../$f" ] && ln -sf "../$f" "$(basename "$f")"
    done
    for s in create_subimage.py create_subimage_rmsy_cube.py run_parallel_rmsy.py \
             merge_image_parts.py config_parser.py region_parser.py cube_validator.py fullSummary; do
        [ -f "../$s" ] && ln -sf "../$s" "$s"
    done

    # Stage 1 (per region): extract cropped Stokes I/Q/U from the full IQUV cube
    R_BASE=$(basename "$FITS_FULL" .fits)
    R_FITS_Q="${R_BASE}.stokesQ.fits"
    R_FITS_U="${R_BASE}.stokesU.fits"
    R_FITS_I="${R_BASE}.stokesI.fits"
    if has_stage extract; then
        if [ "$RESUME" = "True" ] && [ -f "$R_FITS_Q" ] && [ -f "$R_FITS_U" ] && [ -f "$R_FITS_I" ]; then
            echo "[r${RID} Stage 1] SKIP — Stokes Q/U/I already extracted."
        else
            echo "[r${RID} Stage 1] Extracting Stokes I/Q/U with crop=${CROP} pointing=${POINT}"
            singularity --quiet exec "$RM_CONTAINER" python3 ./create_subimage.py \
                --inputcube "$FITS_FULL" --crop "${CROP}" --pointing "${POINT}"
        fi
    else
        echo "[r${RID} Stage 1] Skipping extract ([run] stages=$STAGES)"
    fi

    # Stage 1.5 (per region): noise map (gated by 'extract' stage membership)
    R_NOISE_MAP="noise_map.fits"
    SLURMID_NOISE=""
    if ! has_stage extract; then
        echo "[r${RID} Stage 1.5] Skipping noise map ([run] stages=$STAGES)"
    elif [ -n "$NOISE_MAP_CFG" ]; then
        echo "[r${RID} Stage 1.5] Using pre-supplied noise map: $NOISE_MAP_CFG"
        R_NOISE_MAP="$NOISE_MAP_CFG"
    elif awk "BEGIN{exit !($THRESHOLD < 0)}"; then
        if [ "$RESUME" = "True" ] && [ -f "$R_NOISE_MAP" ]; then
            echo "[r${RID} Stage 1.5] $R_NOISE_MAP already exists -- skipping noise generation."
        else
            echo "[r${RID} Stage 1.5] Submitting make_noise.sbatch (sigma mode)"
            cat > make_noise.sbatch <<NOISEEOF
#!/bin/bash
#SBATCH --nodes=${NODES}
#SBATCH --ntasks-per-node=${NTASKS_PER_NODE}
#SBATCH --cpus-per-task=${CPUS_PER_TASK}
#SBATCH --mem=${MEM_NOISE}GB
#SBATCH --job-name=noise${SUFFIX}
#SBATCH --output=logs/noise-%j.out
#SBATCH --error=logs/noise-%j.err
#SBATCH --partition=${PARTITION}
#SBATCH --time=${TIME}
#SBATCH --account=$ACCOUNT

set -e
export PYTHONDONTWRITEBYTECODE=1
cd "$WORKDIR/$REGION_DIR"
singularity --quiet exec "$RM_CONTAINER" python -m RMtools_3D.make_noise_map \
    "$R_FITS_Q" "$R_FITS_U" -o "$R_NOISE_MAP" -B "$FITS_FULL" -v
NOISEEOF
            SLURMID_NOISE=$(sbatch make_noise.sbatch | awk '{print $4}')
            ALL_NOISE_IDS="$ALL_NOISE_IDS $SLURMID_NOISE"
            echo "[r${RID} Stage 1.5] Noise-map job: SLURM $SLURMID_NOISE"
        fi
    else
        echo "[r${RID} Stage 1.5] Absolute-threshold mode; skipping noise stage."
        R_NOISE_MAP=""
    fi

    # Stage 2 (per region): generate rmsy sbatch
    echo "[r${RID} Stage 2] Generating run_parallel_rmsy.sbatch (region $RID)"
    singularity --quiet exec "$RM_CONTAINER" python3 ./run_parallel_rmsy.py --parallel "$CHUNKS" \
        --inputFitsStokesQ "$R_FITS_Q" \
        --inputFitsStokesU "$R_FITS_U" \
        --freqList "$(basename "$FREQLIST")" \
        --rmsyCleanThrethold "$THRESHOLD" \
        --rmsyCleanIterations "$ITERATIONS" \
        --rmsyCleanWindow "$WINDOW" \
        --rmsyCleanGain "$GAIN" \
        --phimax "$PHIMAX" \
        --dphi "$DPHI" \
        --nsamples "$NSAMPLES" \
        --weightType "$WEIGHTTYPE" \
        --fitGaussianRmsf "$FIT_GAUSSIAN_RMSF" \
        --superResolution "$SUPER_RESOLUTION" \
        --skipRmsf "$SKIP_RMSF" \
        --account "$ACCOUNT" \
        --casaContainer "$CASA_CONTAINER" \
        --rmContainer "$RM_CONTAINER" \
        --noiseMap "$R_NOISE_MAP" \
        --jobNameSuffix "$SUFFIX" \
        --mem "$MEM" \
        --ncores "$NCORES" \
        --internalChunk "$INTERNAL_CHUNK" \
        --partition "$PARTITION" \
        --time "$TIME" \
        --nodes "$NODES" \
        --ntasksPerNode "$NTASKS_PER_NODE" \
        --cpusPerTask "$CPUS_PER_TASK" \
        --stages "$STAGES" \
        --resume "$RESUME" \
        --createSbatch

    # Stage 3 (per region): submit rmsy array (depends on noise stage if any).
    SLURMID_RMSY=""
    DEP_RMSY=""
    # --kill-on-invalid-dep=yes so rmsy self-cancels if the per-region noise
    # job fails, instead of squatting in the queue forever.
    [ -n "$SLURMID_NOISE" ] && DEP_RMSY="--dependency=afterok:$SLURMID_NOISE --kill-on-invalid-dep=yes"
    if ! has_stage rmsynth && ! has_stage rmclean; then
        echo "[r${RID} Stage 3] Skipping RM synthesis array ([run] stages=$STAGES)"
    elif [ "$RESUME" = "True" ]; then
        MISSING=""
        for i in $(seq 1 $CHUNKS); do
            [ -f "processing/part_${i}_FDF_clean_tot.fits" ] || MISSING="${MISSING}${i},"
        done
        MISSING="${MISSING%,}"
        NUM_MISSING=$([ -z "$MISSING" ] && echo 0 || echo "$MISSING" | tr ',' '\n' | wc -l)
        if [ "$NUM_MISSING" = "0" ]; then
            echo "[r${RID} Stage 3] All $CHUNKS chunks already complete -- skipping rmsy submission."
        elif [ "$NUM_MISSING" = "$CHUNKS" ]; then
            echo "[r${RID} Stage 3] All $CHUNKS chunks need processing; submitting full array..."
            SLURMID_RMSY=$(sbatch $DEP_RMSY run_parallel_rmsy.sbatch | awk '{print $4}')
        else
            echo "[r${RID} Stage 3] $NUM_MISSING of $CHUNKS chunks missing; submitting trimmed array..."
            SLURMID_RMSY=$(sbatch $DEP_RMSY --array="$MISSING" run_parallel_rmsy.sbatch | awk '{print $4}')
        fi
    else
        echo "[r${RID} Stage 3] RESUME=False -> submitting full array (no pre-scan)..."
        SLURMID_RMSY=$(sbatch $DEP_RMSY run_parallel_rmsy.sbatch | awk '{print $4}')
    fi
    [ -n "$SLURMID_RMSY" ] && ALL_RMSY_IDS="$ALL_RMSY_IDS $SLURMID_RMSY"
    echo "[r${RID} Stage 3] RM synthesis array: SLURM job ${SLURMID_RMSY:-(none submitted)} ${DEP_RMSY:+(waits on $SLURMID_NOISE)}"

    # Stage 4 (per region): merge-prep dependent on this region's rmsy completing.
    # The merge step uses the input cube's NAXIS1/NAXIS2 to allocate its output
    # cubes, so we point it at the per-region CROPPED Stokes Q -- not the full
    # IQUV cube -- otherwise it allocates 6144x6144 outputs and explodes on
    # broadcasting the 422-wide chunks into them.
    INPUT_CUBE="$R_FITS_Q"
    cat > merge_prep.sbatch <<MPEOF
#!/bin/bash
#SBATCH --nodes=${NODES}
#SBATCH --ntasks-per-node=${NTASKS_PER_NODE}
#SBATCH --cpus-per-task=${CPUS_PER_TASK}
#SBATCH --mem=4GB
#SBATCH --job-name=merge_prep${SUFFIX}
#SBATCH --output=logs/merge_prep-%j.out
#SBATCH --error=logs/merge_prep-%j.err
#SBATCH --partition=${PARTITION}
#SBATCH --time=00:30:00
#SBATCH --account=$ACCOUNT

set -e
export PYTHONDONTWRITEBYTECODE=1
cd "$WORKDIR/$REGION_DIR"

echo "[r${RID} merge_prep] Generating merge_image_parts.sbatch..."
singularity --quiet exec "$RM_CONTAINER" python3 -c "
import sys
sys.path.insert(0, '.')
import merge_image_parts as m
m.write_sbatch_file('$INPUT_CUBE',
                    account='$ACCOUNT',
                    rm_container='$RM_CONTAINER',
                    casa_container='$CASA_CONTAINER',
                    job_name_suffix='${SUFFIX}',
                    mem='$MEM_MERGE',
                    time='$TIME_MERGE',
                    partition='$PARTITION',
                    nodes='$NODES',
                    ntasks_per_node='$NTASKS_PER_NODE',
                    cpus_per_task='$CPUS_PER_TASK',
                    run_fits2idia='$RUN_FITS2IDIA',
                    fix_invalid_stokes='$FIX_INVALID_STOKES')
"

if [ -f merge_image_parts.sbatch ]; then
    SLURMID_MERGE=\$(sbatch merge_image_parts.sbatch | awk '{print \$4}')
    echo "[r${RID} merge_prep] Submitted merge array: \$SLURMID_MERGE"

    # Per-region finalize: clean casa-*.log out of this region subdir once
    # all of its merges have run (afterany so it cleans even on partial fail).
    cat > finalize.sbatch <<FINEOF
#!/bin/bash
#SBATCH --nodes=${NODES}
#SBATCH --ntasks-per-node=${NTASKS_PER_NODE}
#SBATCH --cpus-per-task=${CPUS_PER_TASK}
#SBATCH --mem=1GB
#SBATCH --job-name=finalize${SUFFIX}
#SBATCH --output=logs/finalize-%j.out
#SBATCH --error=logs/finalize-%j.err
#SBATCH --partition=${PARTITION}
#SBATCH --time=00:05:00
#SBATCH --account=$ACCOUNT

set -e
cd "$WORKDIR/$REGION_DIR"
echo "[r${RID} finalize] removing casa-*.log files from \$(pwd)..."
rm -fv casa-*.log 2>/dev/null || true
echo "[r${RID} finalize] done."
FINEOF
    SLURMID_FINALIZE=\$(sbatch --dependency=afterany:\$SLURMID_MERGE finalize.sbatch | awk '{print \$4}')
    echo "[r${RID} merge_prep] Submitted finalize: \$SLURMID_FINALIZE (cleans casa logs)"
else
    echo "[r${RID} merge_prep] WARNING: merge_image_parts.sbatch was not written."
fi
MPEOF
    # Gate merge submission on [merge] run_merge AND [run] stages containing 'merge'.
    if [ "$RUN_MERGE" = "True" ] && has_stage merge; then
        DEP_MERGE=""
        [ -n "$SLURMID_RMSY" ] && DEP_MERGE="--dependency=afterok:$SLURMID_RMSY --kill-on-invalid-dep=yes"
        SLURMID_MERGEPREP=$(sbatch $DEP_MERGE merge_prep.sbatch | awk '{print $4}')
        echo "[r${RID} Stage 4] Merge prep: SLURM job $SLURMID_MERGEPREP ${DEP_MERGE:+(depends on $SLURMID_RMSY)}"
        ALL_MERGEPREP_IDS="$ALL_MERGEPREP_IDS $SLURMID_MERGEPREP"
    else
        echo "[r${RID} Stage 4] Skipping merge ([merge] run_merge=$RUN_MERGE, [run] stages=$STAGES)"
    fi

    cd "$WORKDIR"
done

# Per-stage kill scripts (top-level, cover every region)
write_kill () {
    local script="$1"; local ids="$2"; local label="$3"
    cat > "$script" <<EOF
#!/bin/bash
set -e
IDS="$ids"
[ -z "\$IDS" ] && { echo "No jobs to cancel for: ${label}"; exit 0; }
echo "Cancelling \$IDS (${label})"
scancel \$IDS
EOF
    chmod +x "$script"
}
write_kill "killJobs_noise"         "$ALL_NOISE_IDS" "noise-map jobs (all regions)"
write_kill "killJobs_rmsynth_clean" "$ALL_RMSY_IDS" "RM synthesis + clean arrays (all regions)"
write_kill "killJobs_merge"         "$ALL_MERGEPREP_IDS" "merge prep jobs (will cascade to merge arrays)"
write_kill "killJobs"               "$SLURM_JOB_ID $ALL_NOISE_IDS $ALL_RMSY_IDS $ALL_MERGEPREP_IDS" "all processRM stages (all regions)"

echo ""
echo "===================================="
echo "  Multi-region pipeline submitted!"
echo "===================================="
echo "Regions:           ${#REGION_IDS[@]}"
echo "RM synthesis IDs:  $ALL_RMSY_IDS"
echo "Merge prep IDs:    $ALL_MERGEPREP_IDS"
"""


def generate_submit_script(workdir, config_path, regions=None):
    """Generate the orchestrator sbatch and the thin submit_pipeline.sh wrapper.

    Mirrors processMeerKAT's design: submit_pipeline.sh only calls 'sbatch'
    (works from the login node). The actual orchestration (Stokes extraction,
    run_parallel_rmsy sbatch generation + sub-submission, merge sbatch
    generation + sub-submission) lives inside processRM_orchestrate.sbatch,
    which SLURM dispatches to a compute node where 'singularity exec' is
    permitted.

    If ``regions`` is a non-empty list of dicts (from region_parser), the
    orchestrator instead loops over each region: creates ``region<N>/``,
    extracts a cropped Q/U for that region, sub-submits its own RM-synth
    array, and chains the per-region merge_prep after it. Each region is a
    self-contained mini-workdir under the top-level workdir, so per-region
    outputs and logs never collide.
    """
    regions = regions or []
    orch_path = os.path.join(workdir, 'processRM_orchestrate.sbatch')
    submit_path = os.path.join(workdir, MASTER_SCRIPT)

    # ----- orchestrator sbatch (runs on a compute node) -----
    # SLURM resource directives are templated from the config at generation time
    # (the orchestrator can't call read_cfg before it starts).
    orchestrator = rf"""#!/bin/bash
#SBATCH --nodes=__NODES__
#SBATCH --ntasks-per-node=__NTASKS_PER_NODE__
#SBATCH --cpus-per-task=__CPUS_PER_TASK__
#SBATCH --mem=10GB
#SBATCH --job-name=processRM_orchestrate
#SBATCH --output=logs/orchestrate-%j.out
#SBATCH --error=logs/orchestrate-%j.err
#SBATCH --partition=__PARTITION__
#SBATCH --time=02:00:00
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
    # Pass the default via sys.argv (not inline interpolation) so defaults
    # containing single quotes -- e.g. "['extract', 'rmsynth', ...]" -- don't
    # collide with the surrounding 'quoted' Python literal.
    singularity --quiet exec __RM_CONTAINER__ python3 -c "
import sys, config_parser
t,_ = config_parser.parse_config('$CONFIG')
v = t.get('$1', {{}}).get('$2', '')
s = '' if v is None else str(v).strip().strip(\"'\\\"\")
print(s or sys.argv[1])
" "$3"
}}

FITS_FULL=$(read_cfg data fits_full '')
FREQLIST=$(read_cfg data freqlist '')
NOISE_MAP_CFG=$(read_cfg noise noise_map '')
PARALLEL=$(read_cfg chunking parallel 100)
CHUNKS=$(read_cfg chunking chunks $PARALLEL)
ACCOUNT=$(read_cfg slurm account b03-idia-ag)
RM_CONTAINER=$(read_cfg slurm rm_container '')
CASA_CONTAINER=$(read_cfg slurm casa_container /idia/software/containers/casa-6.4.4-modular.simg)
THRESHOLD=$(read_cfg rmclean threshold 0.0000010)
ITERATIONS=$(read_cfg rmclean iterations 5000)
WINDOW=$(read_cfg rmclean window 0)
GAIN=$(read_cfg rmclean gain 0.1)
PHIMAX=$(read_cfg rmsynth phimax 1000)
DPHI=$(read_cfg rmsynth dphi '')
NSAMPLES=$(read_cfg rmsynth nsamples '')
WEIGHTTYPE=$(read_cfg rmsynth weighttype uniform)
FIT_GAUSSIAN_RMSF=$(read_cfg rmsynth fit_gaussian_rmsf True)
SKIP_RMSF=$(read_cfg rmsynth skip_rmsf False)
SUPER_RESOLUTION=$(read_cfg rmsynth super_resolution False)
MEM=$(read_cfg slurm mem 10)
MEM_NOISE=$(read_cfg slurm mem_noise 100)
MEM_MERGE=$(read_cfg slurm mem_merge 100)
TIME=$(read_cfg slurm time 10:00:00)
TIME_MERGE=$(read_cfg slurm time_merge 20:00:00)
PARTITION=$(read_cfg slurm partition Main)
NODES=$(read_cfg slurm nodes 1)
NTASKS_PER_NODE=$(read_cfg slurm ntasks_per_node 1)
CPUS_PER_TASK=$(read_cfg slurm cpus_per_task 1)
NCORES=$(read_cfg rmclean ncores 1)
INTERNAL_CHUNK=$(read_cfg rmclean internal_chunk '')

# Merge stage flags
RUN_MERGE=$(read_cfg merge run_merge True)
RUN_FITS2IDIA=$(read_cfg merge run_fits2idia True)
FIX_INVALID_STOKES=$(read_cfg merge fix_invalid_stokes True)

# Run-control flags
STAGES=$(read_cfg run stages "['extract', 'rmsynth', 'rmclean', 'merge']")
CONTINUE=$(read_cfg run continue True)
RESUME_SAFE=$(read_cfg run resume_safe True)
# A stage's outputs are reused only when BOTH 'continue' and 'resume_safe' are True.
if [ "$CONTINUE" = "True" ] && [ "$RESUME_SAFE" = "True" ]; then
    RESUME="True"
else
    RESUME="False"
fi
has_stage () {{
    # Returns 0 if $1 appears in $STAGES (which is the literal Python list repr).
    case "$STAGES" in
        *\'$1\'*) return 0 ;;
        *) return 1 ;;
    esac
}}

echo "RM container:   $RM_CONTAINER"
echo "CASA container: $CASA_CONTAINER"
echo ""

__REGION_BLOCK__
"""

    # Templated values not safe to drop straight into f-string above:
    # they need to be substituted AFTER parsing the live config at generation time.
    # We read them from the config here (login-side) and bake into the orchestrator.
    taskvals, _ = config_parser.parse_config(config_path)
    account_val = (taskvals.get('slurm', {}).get('account') or 'b03-idia-ag').strip("'\"")
    rm_container_val = (taskvals.get('slurm', {}).get('rm_container') or '').strip("'\"")
    partition_val = str(taskvals.get('slurm', {}).get('partition') or 'Main').strip("'\"") or 'Main'
    nodes_val = str(taskvals.get('slurm', {}).get('nodes') or '1').strip("'\"") or '1'
    ntasks_val = str(taskvals.get('slurm', {}).get('ntasks_per_node') or '1').strip("'\"") or '1'
    cpus_val = str(taskvals.get('slurm', {}).get('cpus_per_task') or '1').strip("'\"") or '1'
    region_block = _multi_region_body(regions) if regions else _SINGLE_CUBE_BODY
    orchestrator = (orchestrator
                    .replace('__ACCOUNT__', account_val)
                    .replace('__RM_CONTAINER__', rm_container_val)
                    .replace('__PARTITION__', partition_val)
                    .replace('__NODES__', nodes_val)
                    .replace('__NTASKS_PER_NODE__', ntasks_val)
                    .replace('__CPUS_PER_TASK__', cpus_val)
                    .replace('__REGION_BLOCK__', region_block))

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


def _archive_previous_logs(workdir):
    """Move stale .err / .out / timings.csv (and the contents of
    errors/<stage>/) into archive_<timestamp>/ subdirs so a fresh -R run's
    fullSummary report (errors AND per-stage timings) reflects only what
    this submission produces.

    Walks the top-level workdir AND any region<N>/ subdirs created by a
    previous multi-region run. Nothing is deleted -- just moved. If there
    are no stale files anywhere, the function is a no-op.
    """
    ts = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    bases = [workdir] + sorted(glob.glob(os.path.join(workdir, 'region[0-9]*')))
    archived = []
    for base in bases:
        logs_dir = os.path.join(base, 'logs')
        errors_dir = os.path.join(base, 'errors')

        # Move logs/*.err, logs/*.out, and logs/timings.csv. The per-stage
        # timings table in fullSummary appends one row per task per
        # submission, so without rolling it over each run the table shows
        # the cumulative history across every resubmit and confuses the
        # user about what THIS run actually did.
        log_files = []
        if os.path.isdir(logs_dir):
            log_files = (glob.glob(os.path.join(logs_dir, '*.err')) +
                         glob.glob(os.path.join(logs_dir, '*.out')))
            timings_csv = os.path.join(logs_dir, 'timings.csv')
            if os.path.exists(timings_csv):
                log_files.append(timings_csv)

        # Move errors/<stage>/* (the orchestrator pre-creates errors/<stage>/
        # subdirs which may be empty; only sweep files inside them).
        error_files = []
        if os.path.isdir(errors_dir):
            for entry in os.listdir(errors_dir):
                entry_path = os.path.join(errors_dir, entry)
                if entry.startswith('archive_'):
                    continue
                if os.path.isdir(entry_path):
                    error_files.extend(glob.glob(os.path.join(entry_path, '*')))

        if not log_files and not error_files:
            continue

        if log_files:
            archive_logs = os.path.join(logs_dir, f'archive_{ts}')
            os.makedirs(archive_logs, exist_ok=True)
            for f in log_files:
                shutil.move(f, archive_logs)
        if error_files:
            archive_errors = os.path.join(errors_dir, f'archive_{ts}')
            os.makedirs(archive_errors, exist_ok=True)
            for f in error_files:
                shutil.move(f, archive_errors)

        archived.append(os.path.relpath(base, workdir) or '.')
    if archived:
        logger.info(f"  -> archived previous logs (in {', '.join(archived)}) "
                    f"to archive_{ts}/")


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
    # region_file is the only [data] entry that's allowed to be empty (full-cube
    # processing), so it's handled alongside the required cube/freqlist paths
    # only when set.
    missing = []
    data_keys = ['fits_full', 'freqlist']
    if (data.get('region_file') or '').strip():
        data_keys.append('region_file')
    for key in data_keys:
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
    for key in data_keys:
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

    # ---- Re-run cube/freqlist validation against the now-symlinked files ----
    # The user may have edited the config after BUILD, so verify the live state.
    # All FITS-touching work runs inside the rm-env container.
    all_now, _ = config_parser.parse_config(config_path)
    data_now = all_now.get('data', {})
    primary_basename = (data_now.get('fits_full') or '').strip()
    freqlist_basename = (data_now.get('freqlist') or '').strip()
    rm_container_now = (all_now.get('slurm', {}).get('rm_container') or '').strip() \
                       or os.path.join(SCRIPT_DIR, 'container', 'rm-env.sif')
    # [CHANGE 2026-06-25]: prefer the SLURM account written in the config for
    # the srun-wrapped validation calls below. _resolve_slurm_account warns
    # (once) and falls back to the user's DefaultAccount if it isn't in the
    # valid-accounts list.
    account_now = (all_now.get('slurm', {}).get('account') or '').strip() or None

    if primary_basename and freqlist_basename:
        primary_path = os.path.join(workdir, primary_basename)
        freqlist_path = os.path.join(workdir, freqlist_basename)
        info = None
        try:
            info = cube_validator.validate_freqlist_against_cube(primary_path, freqlist_path)
        except cube_validator.CubeStructureError as e:
            if 'astropy is required' in str(e):
                # Fall back to the container
                try:
                    info = _validate_cube_via_container(
                        rm_container_now, primary_path, freqlist_path,
                        account=account_now,
                    )
                except FileNotFoundError as fe:
                    logger.error(f"Cannot validate cube: container not found at {fe}. "
                                 "Run setup.sh to fetch it.")
                    cleanup_run_artifacts(workdir, keep_config=config_path)
                    sys.exit(1)
                except RuntimeError as re_err:
                    logger.error(f"Cube validation failed inside the container: {re_err}")
                    cleanup_run_artifacts(workdir, keep_config=config_path)
                    sys.exit(1)
                except cube_validator.CubeStructureError as ce:
                    logger.error(str(ce))
                    cleanup_run_artifacts(workdir, keep_config=config_path)
                    sys.exit(1)
            else:
                logger.error(str(e))
                cleanup_run_artifacts(workdir, keep_config=config_path)
                sys.exit(1)
        if info and info.get('needs_transpose'):
            logger.warning(
                f"  -> auto-transposing axes (RA,DEC,STOKES,FREQ) -> (RA,DEC,FREQ,STOKES) "
                f"on a local copy of: {primary_basename}"
            )
            # Replace the symlink with a real copy first so the rewrite does NOT
            # propagate through to the user's original cube on /idia/ or /users/.
            try:
                if os.path.islink(primary_path):
                    source = os.path.realpath(primary_path)
                    os.unlink(primary_path)
                    shutil.copy2(source, primary_path)
                try:
                    cube_validator.transpose_cube_in_place(primary_path, info)
                except (ImportError, ModuleNotFoundError):
                    _transpose_cube_via_container(rm_container_now, primary_path,
                                                  account=account_now)
            except Exception as e:
                logger.error(f"Auto-transpose failed: {e}")
                cleanup_run_artifacts(workdir, keep_config=config_path)
                sys.exit(1)

        # Hard beam-info gate: if the (possibly user-edited) config still asks
        # for sigma cleaning and the cube has no beam metadata, refuse to
        # submit; the noise stage would crash inside SLURM.
        _check_beam_for_sigma(primary_path, all_now, config_path, hard=True,
                              rm_container_path=rm_container_now,
                              account=account_now)

    # Subdirectories — only now that we know we will actually run
    for sub in ['logs', 'processing', 'errors']:
        os.makedirs(os.path.join(workdir, sub), exist_ok=True)
    for sub in ['extract', 'rmsynth', 'rmclean', 'merge']:
        os.makedirs(os.path.join(workdir, 'errors', sub), exist_ok=True)

    copy_pipeline_scripts(workdir)
    for support in ('config_parser.py', 'region_parser.py', 'cube_validator.py'):
        shutil.copy2(os.path.join(SCRIPT_DIR, support),
                     os.path.join(workdir, support))

    # ---- Parse the region file (now that the cube and region file both
    # live in workdir) so the orchestrator script can bake in one entry
    # per region. If region_file is empty, regions=[] -> single-cube flow.
    # World-coord regions need the cube's WCS, so the parse runs inside the
    # container.
    regions = []
    region_file_basename = (data_now.get('region_file') or '').strip()
    if region_file_basename and primary_basename:
        region_full = os.path.join(workdir, region_file_basename)
        cube_full = os.path.join(workdir, primary_basename)
        wcs_header = None
        try:
            from astropy.io import fits as _fits
            wcs_header = _fits.getheader(cube_full)
        except Exception:
            pass
        try:
            regions = region_parser.parse_region_file(region_full, wcs_header)
        except region_parser.RegionParseError as e:
            if 'no cube WCS was supplied' in str(e):
                try:
                    regions = _parse_region_via_container(
                        rm_container_now, region_full, cube_full,
                        account=account_now,
                    )
                except FileNotFoundError as fe:
                    logger.error(f"Cannot parse region file: container not found at {fe}.")
                    cleanup_run_artifacts(workdir, keep_config=config_path)
                    sys.exit(1)
                except RuntimeError as re_err:
                    logger.error(f"Region parsing failed inside the container: {re_err}")
                    cleanup_run_artifacts(workdir, keep_config=config_path)
                    sys.exit(1)
            else:
                logger.error(f"Region file '{region_file_basename}': {e}")
                cleanup_run_artifacts(workdir, keep_config=config_path)
                sys.exit(1)
        logger.info(f"will run {len(regions)} region(s):")
        for line in region_parser.describe_regions(regions):
            logger.info(f"  -> {line}")

    # Stash any leftover .err/.out files from previous submissions so
    # fullSummary's error report reflects only what this run produces.
    _archive_previous_logs(workdir)

    return generate_submit_script(workdir, config_path, regions=regions)


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
    #   -B BUILD mode: only writes/updates the config file
    #   -R RUN mode:   reads the config, creates symlinks, copies scripts,
    #                  generates submit_pipeline.sh, and (with -s) submits.
    # The two modes are mutually exclusive.
    if args.build and args.run_config:
        logger.error("Cannot use -B (build) and -R (run) at the same time.")
        sys.exit(1)

    if args.build:
        if not args.fitsfile:
            logger.error("-B (build) requires -F <full-Stokes IQUV cube>.")
            logger.error("Try 'processRM --help' for examples.")
            sys.exit(1)
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
        if args.fitsfile:
            logger.error("-F was given but -B (build) is required to enter BUILD mode.")
            logger.error("Try: processRM -B -F <cube.fits> -f <freqs.txt>")
        else:
            logger.error("Must provide either -B (build mode) or -R (run mode).")
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
