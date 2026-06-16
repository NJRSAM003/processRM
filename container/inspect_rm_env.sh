#!/bin/bash
# ==================================================================
#  inspect_rm_env.sh - inspect existing RM-env to match in container
#  Made by Amani - Made to make RM-synthesis easier
# ==================================================================
#
# Run this on ilifu to get the exact package list from your existing
# RM-env so we can pin versions in the Singularity definition file.
#
# Usage:  ./inspect_rm_env.sh
# ==================================================================

RM_ENV="$HOME/myvenvs/RM-env"

if [ ! -d "$RM_ENV" ]; then
    echo "ERROR: RM-env not found at $RM_ENV"
    exit 1
fi

echo "=================================================="
echo "  RM-env Inspection (for container build)"
echo "=================================================="
echo ""
echo "Location: $RM_ENV"
echo "Python:   $($RM_ENV/bin/python3 --version)"
echo ""
echo "=================================================="
echo "  Installed packages (pip freeze)"
echo "=================================================="
"$RM_ENV/bin/pip" freeze | tee /tmp/rm_env_freeze.txt

echo ""
echo "=================================================="
echo "  Key binaries"
echo "=================================================="
for binary in rmsynth3d rmclean3d rmsynth1d rmclean1d; do
    if [ -x "$RM_ENV/bin/$binary" ]; then
        echo "  $binary: $($RM_ENV/bin/$binary --version 2>&1 | head -1)"
    fi
done

echo ""
echo "=================================================="
echo "  Output saved to /tmp/rm_env_freeze.txt"
echo "  Copy this and paste into rm-env.def %post section"
echo "=================================================="
