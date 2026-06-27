#!/usr/bin/env bash
# Installs (or re-installs) the WithoutBG GIMP plugin.
# Creates the plug-ins directory if needed and symlinks the source file
# so that edits in the repo are immediately reflected in GIMP.

set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/withoutbg/withoutbg-gimp/main"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
LOCAL_SOURCE="$SCRIPT_DIR/withoutbg/withoutbg.py"

# Detect GIMP user-data directory
if [[ -d "$HOME/Library/Application Support/GIMP/3.0" ]]; then
    GIMP_DIR="$HOME/Library/Application Support/GIMP/3.0"   # macOS
elif [[ -d "$HOME/.config/GIMP/3.0" ]]; then
    GIMP_DIR="$HOME/.config/GIMP/3.0"                       # Linux
else
    echo "ERROR: Could not find GIMP 3.0 user directory." >&2
    echo "  Expected one of:" >&2
    echo "    ~/Library/Application Support/GIMP/3.0   (macOS)" >&2
    echo "    ~/.config/GIMP/3.0                       (Linux)" >&2
    exit 1
fi

PLUGIN_DIR="$GIMP_DIR/plug-ins/withoutbg"
TARGET="$PLUGIN_DIR/withoutbg.py"

mkdir -p "$PLUGIN_DIR"
rm -f "$TARGET"

if [[ -f "$LOCAL_SOURCE" ]]; then
    # Local checkout — symlink so repo edits show up in GIMP immediately.
    chmod +x "$LOCAL_SOURCE"
    ln -s "$LOCAL_SOURCE" "$TARGET"
    echo "Installed: $TARGET -> $LOCAL_SOURCE"
else
    # Remote one-line install — download the plugin file.
    curl -fsSL "$REPO_RAW/withoutbg/withoutbg.py" -o "$TARGET"
    chmod +x "$TARGET"
    echo "Installed: $TARGET"
fi
echo
echo "Restart GIMP and look under:  Tools ▸ WithoutBG ▸ Remove Background…"
