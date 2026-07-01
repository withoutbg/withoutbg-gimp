#!/usr/bin/env bash
# Installs (or re-installs) the WithoutBG GIMP plugin.
# Creates the plug-ins directory if needed and symlinks the source file
# so that edits in the repo are immediately reflected in GIMP.

if [ -z "${BASH_VERSION:-}" ]; then
    echo "ERROR: This script requires bash (Ubuntu's sh is dash and is not supported)." >&2
    echo '  bash -c "$(curl -fsSL https://raw.githubusercontent.com/withoutbg/withoutbg-gimp/main/install.sh)"' >&2
    exit 1
fi

set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/withoutbg/withoutbg-gimp/main"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
LOCAL_SOURCE="$SCRIPT_DIR/withoutbg/withoutbg.py"

# Pick the newest GIMP 3.x user-data directory under a base path (macOS or Linux).
_find_gimp3_dir() {
    local base="$1"
    local dir ver best="" best_ver=""
    shopt -s nullglob
    for dir in "$base"/3.*; do
        [[ -d "$dir" ]] || continue
        ver="${dir##*/}"
        if [[ -z "$best_ver" ]] || [[ "$(printf '%s\n%s\n' "$best_ver" "$ver" | sort -V | tail -1)" == "$ver" ]]; then
            best="$dir"
            best_ver="$ver"
        fi
    done
    shopt -u nullglob
    printf '%s' "$best"
}

# Infer GIMP 3.x profile version from the gimp binary (e.g. 3.2.4 -> 3.2).
_gimp_profile_version() {
  if command -v gimp >/dev/null 2>&1; then
    gimp --version 2>/dev/null | sed -n 's/.*version \([0-9][0-9]*\.[0-9][0-9]*\).*/\1/p' | head -1
  fi
}

# Detect GIMP user-data directory
GIMP_DIR="$(_find_gimp3_dir "$HOME/Library/Application Support/GIMP")"   # macOS
if [[ -z "$GIMP_DIR" ]]; then
    GIMP_DIR="$(_find_gimp3_dir "$HOME/.config/GIMP")"                   # Linux
fi
if [[ -z "$GIMP_DIR" ]]; then
    PROFILE_VER="$(_gimp_profile_version)"
    if [[ -n "$PROFILE_VER" ]]; then
        if [[ -d "$HOME/Library/Application Support/GIMP" ]] || [[ "$(uname -s)" == "Darwin" ]]; then
            GIMP_DIR="$HOME/Library/Application Support/GIMP/$PROFILE_VER"
        else
            GIMP_DIR="$HOME/.config/GIMP/$PROFILE_VER"
        fi
        echo "No GIMP profile found yet; using GIMP $PROFILE_VER path: $GIMP_DIR"
    fi
fi
if [[ -z "$GIMP_DIR" ]]; then
    echo "ERROR: Could not find GIMP 3.x user directory." >&2
    echo "  Install GIMP 3.x and run it once, or ensure one of these exists:" >&2
    echo "    ~/Library/Application Support/GIMP/3.x   (macOS)" >&2
    echo "    ~/.config/GIMP/3.x                       (Linux)" >&2
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
