#!/bin/bash
# ==================================================================
#  processRM Setup Script (integrated with ilifu)
#  Made by Amani - Made to make RM-synthesis easier
# ==================================================================
#
# This script adds processRM to your PATH so it can be invoked
# from anywhere as: processRM -F file.fits -f freqs.txt
#
# Usage:  ./setup.sh
#

set -e

PROCESSRM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHELL_RC=""

# Detect shell config file
if [ -n "$BASH_VERSION" ]; then
    SHELL_RC="$HOME/.bashrc"
elif [ -n "$ZSH_VERSION" ]; then
    SHELL_RC="$HOME/.zshrc"
else
    if [ -f "$HOME/.bashrc" ]; then
        SHELL_RC="$HOME/.bashrc"
    elif [ -f "$HOME/.zshrc" ]; then
        SHELL_RC="$HOME/.zshrc"
    else
        echo "ERROR: Could not detect shell config (.bashrc or .zshrc)"
        exit 1
    fi
fi

echo "=================================================="
echo "  processRM Setup (integrated with ilifu)"
echo "  Made by Amani - Made to make RM-synthesis easier"
echo "=================================================="
echo ""
echo "Install location: $PROCESSRM_DIR"
echo "Shell config:     $SHELL_RC"
echo ""

# Make main scripts executable
chmod +x "$PROCESSRM_DIR/processRM.py"
chmod +x "$PROCESSRM_DIR/config_parser.py"
chmod +x "$PROCESSRM_DIR/aux_scripts/fullSummary"
chmod +x "$PROCESSRM_DIR/templates/"*.py

# Create symlink alias
PROCESSRM_CMD="$PROCESSRM_DIR/processRM.py"

# Add to PATH and create alias if not already present
if grep -q "# processRM setup" "$SHELL_RC" 2>/dev/null; then
    echo "  -> processRM already configured in $SHELL_RC"
else
    cat >> "$SHELL_RC" <<EOF

# processRM setup (integrated with ilifu) - Made by Amani
export PROCESSRM_DIR="$PROCESSRM_DIR"
alias processRM="\$PROCESSRM_DIR/processRM.py"
EOF
    echo "  -> Added processRM to $SHELL_RC"
fi

echo ""
echo "=================================================="
echo "  Setup complete!"
echo "=================================================="
echo ""
echo "  To activate, run:"
echo "      source $SHELL_RC"
echo ""
echo "  Then verify:"
echo "      processRM --help"
echo ""
echo "  Example usage:"
echo "      processRM -F mycube_IQUV.fits -f freqs.txt"
echo "      processRM -F \"Q.fits U.fits\" -f freqs.txt --chunks 50"
echo "      processRM -C myconfig.txt -s"
echo ""
echo "=================================================="
