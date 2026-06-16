#!/bin/bash
# ==================================================================
#  build_container.sh - Build the RM-env Singularity container
#  Made by Amani - Made to make RM-synthesis easier
# ==================================================================
#
# IMPORTANT: ilifu does NOT support building Singularity containers
# directly (no sudo, no fakeroot dependencies, login/transfer nodes
# block 'singularity build' outright).
#
# You must build the container in ONE of these places, then upload it:
#
#   A) Sylabs Cloud (recommended — no local install needed)
#      1. Sign up: https://cloud.sylabs.io/
#      2. Generate an access token
#      3. From ANY machine with singularity:
#           singularity remote login
#           singularity build --remote rm-env.sif rm-env.def
#
#   B) A local Linux machine where you have sudo
#      1. Install singularity (or singularity-ce)
#      2. Run:
#           sudo singularity build rm-env.sif rm-env.def
#
# After building, upload to ilifu:
#   scp rm-env.sif amani@transfer.ilifu.ac.za:/idia/projects/<your-project>/containers/
# ==================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEF_FILE="$SCRIPT_DIR/rm-env.def"
SIF_FILE="$SCRIPT_DIR/rm-env.sif"

if [ ! -f "$DEF_FILE" ]; then
    echo "ERROR: Definition file not found: $DEF_FILE"
    exit 1
fi

# Refuse to build on ilifu (it won't work)
HOSTNAME_SHORT=$(hostname -s 2>/dev/null || hostname)
if [[ "$HOSTNAME_SHORT" == slurm-login* ]] || \
   [[ "$HOSTNAME_SHORT" == transfer* ]] || \
   [[ "$HOSTNAME_SHORT" == compute-* ]] || \
   [[ -d /idia/software ]]; then
    cat <<EOF
==================================================================
  Cannot build the container on ilifu.
==================================================================
ilifu does not allow 'singularity build' (no sudo, missing fakeroot
dependencies, login/transfer nodes block it outright).

Please build the container elsewhere:

  Option A) Sylabs Cloud (recommended)
    1. Sign up at https://cloud.sylabs.io/ and create an access token.
    2. On any machine with singularity installed, run:
         singularity remote login
         singularity build --remote rm-env.sif rm-env.def

  Option B) Local Linux machine with sudo
    1. Install singularity (or singularity-ce).
    2. Run:
         sudo singularity build rm-env.sif rm-env.def

Once you have rm-env.sif, upload it to ilifu:
  scp rm-env.sif amani@transfer.ilifu.ac.za:/idia/projects/<your-project>/containers/

Then update [slurm] rm_container in your config to that destination.
==================================================================
EOF
    exit 1
fi

# Check singularity is installed
if ! command -v singularity &> /dev/null; then
    echo "ERROR: singularity not found in PATH"
    echo "Install singularity-ce: https://docs.sylabs.io/guides/latest/admin-guide/installation.html"
    exit 1
fi

echo "=================================================="
echo "  Building rm-env.sif Singularity container"
echo "  Made by Amani - Made to make RM-synthesis easier"
echo "=================================================="
echo "Def file: $DEF_FILE"
echo "Output:   $SIF_FILE"
echo ""

# Try build methods in order of likely success on a local machine
echo "Attempting build with --remote (Sylabs Cloud)..."
if singularity build --remote "$SIF_FILE" "$DEF_FILE" 2>/dev/null; then
    echo "  -> SUCCESS via --remote"
elif sudo singularity build "$SIF_FILE" "$DEF_FILE" 2>/dev/null; then
    echo "  -> SUCCESS via sudo"
elif singularity build --fakeroot "$SIF_FILE" "$DEF_FILE"; then
    echo "  -> SUCCESS via --fakeroot"
else
    cat <<EOF

ERROR: All build methods failed.

Try manually:
  singularity remote login    # then:
  singularity build --remote $SIF_FILE $DEF_FILE

OR with sudo:
  sudo singularity build $SIF_FILE $DEF_FILE
EOF
    exit 1
fi

echo ""
echo "=================================================="
echo "  Build complete!"
echo "=================================================="
ls -lh "$SIF_FILE"
echo ""
echo "Next: upload to ilifu and update your config:"
echo "  scp $SIF_FILE amani@transfer.ilifu.ac.za:/idia/projects/<your-project>/containers/"
echo "  # then set [slurm] rm_container to that destination in your config"
echo "=================================================="
