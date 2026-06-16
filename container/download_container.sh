#!/bin/bash
# ==================================================================
#  download_container.sh - Fetch the latest rm-env.sif from GitHub
#  Made by Amani - Made to make RM-synthesis easier
# ==================================================================
#
# Downloads the prebuilt rm-env.sif container from the latest published
# processRM release on GitHub and places it next to this script.
#
# Usage:
#   ./download_container.sh                # latest release
#   ./download_container.sh v1.0           # specific tagged release
# ==================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TAG="${1:-latest}"

if [ "$TAG" = "latest" ]; then
    URL="https://github.com/NJRSAM003/processRM/releases/latest/download/rm-env.sif"
else
    URL="https://github.com/NJRSAM003/processRM/releases/download/${TAG}/rm-env.sif"
fi

DEST="$SCRIPT_DIR/rm-env.sif"

echo "=================================================="
echo "  Downloading rm-env.sif (${TAG})"
echo "=================================================="
echo "URL:  $URL"
echo "Dest: $DEST"
echo ""

if [ -f "$DEST" ]; then
    echo "WARNING: $DEST already exists."
    read -p "Overwrite? [y/N] " ans
    case "$ans" in
        [yY]*) rm -f "$DEST" ;;
        *) echo "Aborted."; exit 0 ;;
    esac
fi

if command -v wget &> /dev/null; then
    wget -O "$DEST" "$URL"
elif command -v curl &> /dev/null; then
    curl -L -o "$DEST" "$URL"
else
    echo "ERROR: neither wget nor curl is installed."
    exit 1
fi

echo ""
echo "=================================================="
echo "  Download complete!"
echo "=================================================="
ls -lh "$DEST"
echo ""
echo "Next steps:"
echo "  1. Move to a SLURM-readable location, e.g.:"
echo "     mv $DEST /idia/projects/<your-project>/containers/rm-env.sif"
echo "  2. Set [slurm] rm_container = '...' in your processRM config."
echo "=================================================="
