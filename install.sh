#!/usr/bin/env bash
# Install brickprint as a global skill for Codex and Claude.
#
#   ./install.sh          copy
#   ./install.sh --link   symlink (development)
#   ./install.sh -h       help
#
# Environment overrides (all optional):
#   BRICKPRINT_CONFIG   config file to write (default ~/.config/brickprint/config.json,
#                       the same path brickprint/config.py reads)
#   BRICKPRINT_CACHE    cache dir written into the config
#   BRICKPRINT_LDRAW    LDraw library folder (the one holding LDConfig.ldr)
#   BRICKPRINT_LDVIEW   LDView executable
#   BRICKPRINT_BAMBU    Bambu Studio executable
#   BRICKPRINT_OS       pretend `uname -s` returned this (for testing: Darwin / Linux)
#
# Compatible with bash 3.2 (macOS /bin/bash).
set -euo pipefail

usage() {
    sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

MODE="copy"
case "${1:-}" in
    "") ;;
    --link) MODE="link" ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
esac

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OS="${BRICKPRINT_OS:-$(uname -s)}"

# Config: brickprint/config.py reads ~/.config/brickprint/config.json on every
# OS (no XDG lookup) unless BRICKPRINT_CONFIG points elsewhere; mirror that.
CONFIG_FILE="${BRICKPRINT_CONFIG:-${HOME}/.config/brickprint/config.json}"
CONFIG_DIR="$(dirname "$CONFIG_FILE")"

# Cache: same defaults as config.default_cache_dir().
if [[ "$OS" == "Darwin" ]]; then
    CACHE_DIR="${HOME}/Library/Caches/brickprint"
else
    CACHE_DIR="${XDG_CACHE_HOME:-${HOME}/.cache}/brickprint"
fi
CACHE_DIR="${BRICKPRINT_CACHE:-$CACHE_DIR}"
BACKUP_ROOT="${CACHE_DIR}/backups"

# --- discovery helpers (print a path, or nothing) --------------------------

first_executable() {          # first_executable PATH...
    local p
    for p in "$@"; do
        if [[ -n "$p" && -f "$p" && -x "$p" ]]; then
            printf '%s' "$p"
            return 0
        fi
    done
    return 1
}

on_path() {                   # on_path NAME...  -> absolute path of first hit
    local n p
    for n in "$@"; do
        if p="$(command -v "$n" 2>/dev/null)" && [[ -n "$p" && -x "$p" ]]; then
            printf '%s' "$p"
            return 0
        fi
    done
    return 1
}

find_ldraw() {
    local cand d
    if [[ -n "${BRICKPRINT_LDRAW:-}" ]]; then
        printf '%s' "$BRICKPRINT_LDRAW"
        return 0
    fi
    if [[ -f "$HERE/ldraw_library/ldraw/LDConfig.ldr" ]]; then
        printf '%s' "$HERE/ldraw_library/ldraw"
        return 0
    fi
    d="$HERE"
    while [[ "$d" != "/" && -n "$d" ]]; do   # same upward search as config.py
        d="$(dirname "$d")"
        if [[ -f "$d/ldraw_library/ldraw/LDConfig.ldr" ]]; then
            printf '%s' "$d/ldraw_library/ldraw"
            return 0
        fi
    done
    for cand in \
        "${HOME}/Library/Application Support/LDraw/ldraw" \
        "/Applications/LDraw/ldraw" \
        "${HOME}/ldraw" \
        "/usr/share/ldraw" \
        "${CACHE_DIR}/ldraw"; do
        if [[ -f "$cand/LDConfig.ldr" ]]; then
            printf '%s' "$cand"
            return 0
        fi
    done
    return 0
}

ancestor_ldview() {           # <ancestor>/tools/LDView.app, like config.py's runtime probe
    local d="$HERE"
    while [[ "$d" != "/" && -n "$d" ]]; do
        d="$(dirname "$d")"
        if [[ -x "$d/tools/LDView.app/Contents/MacOS/LDView" ]]; then
            printf '%s' "$d/tools/LDView.app/Contents/MacOS/LDView"
            return 0
        fi
    done
    return 1
}

find_ldview() {
    if [[ -n "${BRICKPRINT_LDVIEW:-}" ]]; then
        printf '%s' "$BRICKPRINT_LDVIEW"
        return 0
    fi
    if [[ "$OS" == "Darwin" ]]; then
        first_executable \
            "/Applications/LDView.app/Contents/MacOS/LDView" \
            "${HOME}/Applications/LDView.app/Contents/MacOS/LDView" \
            || on_path LDView ldview || ancestor_ldview || true
    else
        on_path LDView ldview \
            || first_executable /usr/bin/LDView /usr/local/bin/LDView /usr/bin/ldview \
                /usr/local/bin/ldview /opt/LDView/LDView || true
    fi
}

find_bambu() {
    if [[ -n "${BRICKPRINT_BAMBU:-}" ]]; then
        printf '%s' "$BRICKPRINT_BAMBU"
        return 0
    fi
    if [[ "$OS" == "Darwin" ]]; then
        first_executable \
            "/Applications/BambuStudio.app/Contents/MacOS/BambuStudio" \
            "${HOME}/Applications/BambuStudio.app/Contents/MacOS/BambuStudio" || true
    else
        on_path bambu-studio BambuStudio \
            || first_executable /usr/bin/bambu-studio /usr/local/bin/bambu-studio \
                /opt/BambuStudio/bambu-studio /opt/bambu-studio/bambu-studio || true
    fi
}

json_escape() {               # escape backslash, quote and control chars
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/	/\\t/g' | tr -d '\r\n'
}

ldraw_dir="$(find_ldraw)"
ldview_bin="$(find_ldview)"
bambu_bin="$(find_bambu)"

if [[ -z "$bambu_bin" && "$OS" != "Darwin" ]] && command -v flatpak >/dev/null 2>&1 \
        && flatpak info com.bambulab.BambuStudio >/dev/null 2>&1; then
    echo "note: Bambu Studio is installed as a flatpak (com.bambulab.BambuStudio);"
    echo "      there is no plain executable to record. Wrap it in a script that runs"
    echo "      'flatpak run com.bambulab.BambuStudio \"\$@\"' and set \"bambu\" (or BRICKPRINT_BAMBU) to it."
fi

# --- install ---------------------------------------------------------------

copy_tree() {                 # copy_tree SRC DEST  (excludes junk)
    local src="$1" dest="$2"
    if command -v rsync >/dev/null 2>&1; then
        rsync -a --exclude '__pycache__' --exclude '.DS_Store' --exclude '.git' --exclude '.gitignore' \
            --exclude '*.egg-info' --exclude '.pytest_cache' --exclude '.ruff_cache' --exclude '.venv' \
            "$src"/ "$dest"/
    else
        (cd "$src" && tar --exclude '__pycache__' --exclude '.DS_Store' --exclude '.git' \
            --exclude '.gitignore' --exclude '*.egg-info' --exclude '.pytest_cache' \
            --exclude '.ruff_cache' --exclude '.venv' -cf - .) | (cd "$dest" && tar -xf -)
    fi
}

install_one() {
    local dest="$1"
    mkdir -p "$(dirname "$dest")"
    if [[ -e "$dest" || -L "$dest" ]]; then
        mkdir -p "$BACKUP_ROOT"
        local backup
        backup="$(mktemp -d "${BACKUP_ROOT}/brickprint.XXXXXX")"
        mv "$dest" "$backup/$(basename "$dest")"
        echo "backed up $dest -> $backup"
    fi
    if [[ "$MODE" == "link" ]]; then
        ln -s "$HERE" "$dest"
        echo "linked $dest -> $HERE"
    else
        mkdir -p "$dest"
        copy_tree "$HERE" "$dest"
        echo "copied $dest"
    fi
    chmod +x "$dest/bin/brickprint" 2>/dev/null || true
}

install_one "${HOME}/.codex/skills/brickprint"
install_one "${HOME}/.claude/skills/brickprint"

mkdir -p "$CONFIG_DIR"
if [[ ! -f "$CONFIG_FILE" ]]; then
    cat > "$CONFIG_FILE" <<JSON
{
 "cache_dir": "$(json_escape "$CACHE_DIR")",
 "ldraw_dir": "$(json_escape "$ldraw_dir")",
 "ldview": "$(json_escape "$ldview_bin")",
 "bambu": "$(json_escape "$bambu_bin")"
}
JSON
    echo "wrote $CONFIG_FILE"
else
    echo "kept existing $CONFIG_FILE"
fi

echo
echo "Installed.  Try:"
echo "  ${HOME}/.codex/skills/brickprint/bin/brickprint doctor"
echo "  ${HOME}/.codex/skills/brickprint/bin/brickprint inventory 10337-1"
echo "  ${HOME}/.codex/skills/brickprint/bin/brickprint build 10337-1 --bambu"
