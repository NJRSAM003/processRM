#!/bin/bash
# ==================================================================
#  build_container.sh - Build the RM-env Singularity container
#  Made by Amani - Made to make RM-synthesis easier
# ==================================================================
#
# Builds rm-env.sif from rm-env.def
#
# Three build options (the script will try them in order):
#   1. sudo singularity build       (requires sudo)
#   2. singularity build --fakeroot (requires fakeroot setup)
#   3. singularity build --remote   (requires Sylabs Cloud account)
#
# Usage:  ./build_container.sh
# ==================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEF_FILE="$SCRIPT_DIR/rm-env.def"
SIF_FILE="$SCRIPT_DIR/rm-env.sif"

if [ ! -f "$DEF_FILE" ]; then
    echo "ERROR: Definition file not found: $DEF_FILE"
    exit 1
fi

echo "=================================================="
echo "  Building rm-env.sif Singularity container"
echo "  Made by Amani - Made to make RM-synthesis easier"
echo "=================================================="
echo "Def file: $DEF_FILE"
echo "Output:   $SIF_FILE"
echo ""

# Check Singularity is installed
if ! command -v singularity &> /dev/null; then
    echo "ERROR: singularity not found in PATH"
    echo "On ilifu, load it with: module load singularity"
    exit 1
fi

# Try fakeroot first (ilifu-friendly)
echo "Attempting build with --fakeroot..."
if singularity build --fakeroot "$SIF_FILE" "$DEF_FILE" 2>/dev/null; then
    echo "  -> SUCCESS via --fakeroot"
elif sudo singularity build "$SIF_FILE" "$DEF_FILE" 2>/dev/null; then
    echo "  -> SUCCESS via sudo"
elif singularity build --remote "$SIF_FILE" "$DEF_FILE"; then
    echo "  -> SUCCESS via --remote"
else
    echo ""
    echo "ERROR: All build methods failed."
    echo ""
    echo "Try one of the following manually:"
    echo "  1. sudo singularity build $SIF_FILE $DEF_FILE"
    echo "  2. singularity build --fakeroot $SIF_FILE $DEF_FILE"
    echo "  3. singularity build --remote $SIF_FILE $DEF_FILE"
    echo ""
    echo "For remote build, sign up at https://cloud.sylabs.io/"
    echo "and run 'singularity remote login' first."
    exit 1
fi

echo ""
echo "=================================================="
echo "  Build complete!"
echo "=================================================="
ls -lh "$SIF_FILE"
echo ""
echo "Test the container:"
echo "  singularity exec $SIF_FILE rmsynth3d --help"
echo ""
echo "Recommended: move to ilifu shared container directory"
echo "  mv $SIF_FILE /idia/projects/<your-project>/containers/"
echo "=================================================="
