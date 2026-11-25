#!/usr/bin/env bash
# Install brickprint as a global skill for Antigravity, Codex, and Claude Code.
# Compatible with bash 3.2 (macOS /bin/bash).
set -euo pipefail

usage() {
    cat <<'EOF'
Install brickprint as a global skill for Antigravity, Codex, and Claude Code.

Usage:
  ./install.sh [options]

Options:
  --link               symlink into skill directories instead of copying (development)
  --keep-backups N     number of brickprint.* backup dirs to keep (default: 6; 0 = keep all)
  -h, --help           show this help

Targets:
  ~/.gemini/config/skills/brickprint   (Antigravity)
  ~/.codex/skills/brickprint           (Codex)
  ~/.claude/skills/brickprint          (Claude Code)

Environment overrides (all optional):
  BRICKPRINT_CONFIG       config file to write (default ~/.config/brickprint/config.json)
  BRICKPRINT_CACHE        cache dir written into the config
  BRICKPRINT_LDRAW        LDraw library folder (the one holding LDConfig.ldr)
  BRICKPRINT_LDVIEW       LDView executable
  BRICKPRINT_BAMBU        Bambu Studio executable
  BRICKPRINT_OS           pretend `uname -s` returned this (for testing: Darwin / Linux)
  BRICKPRINT_KEEP_BACKUPS default for --keep-backups (default: 6)
EOF
}

MODE="copy"
KEEP_BACKUPS="${BRICKPRINT_KEEP_BACKUPS:-6}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --link)
            MODE="link"
            shift
            ;;
        --keep-backups)
            if [[ $# -lt 2 ]] || ! [[ "$2" =~ ^[0-9]+$ ]]; then
                echo "--keep-backups requires a non-negative integer" >&2
                exit 2
            fi
            KEEP_BACKUPS="$2"
            shift 2
            ;;
        --keep-backups=*)
            KEEP_BACKUPS="${1#*=}"
            if ! [[ "$KEEP_BACKUPS" =~ ^[0-9]+$ ]]; then
                echo "--keep-backups requires a non-negative integer" >&2
                exit 2
            fi
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OS="${BRICKPRINT_OS:-$(uname -s)}"

# Config: brickprint/config.py reads ~/.config/brickprint/config.json on every
# OS unless BRICKPRINT_CONFIG points elsewhere; mirror that.
CONFIG_FILE="${BRICKPRINT_CONFIG:-${HOME}/.config/brickprint/config.json}"
CONFIG_DIR="$(dirname "$CONFIG_FILE")"

# Cache: mirrors config.default_cache_dir() (and honors XDG_CACHE_HOME on Linux).
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

ancestor_ldview() {           # <ancestor>/tools/LDView.app, matching config.py
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
        ancestor_ldview \
            || first_executable \
                "/Applications/LDView.app/Contents/MacOS/LDView" \
                "${HOME}/Applications/LDView.app/Contents/MacOS/LDView" \
            || on_path LDView ldview || true
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
            "${HOME}/Applications/BambuStudio.app/Contents/MacOS/BambuStudio" \
            || on_path BambuStudio bambu-studio || true
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

copy_tree() {                 # copy_tree SRC DEST  (excludes dev/cache junk)
    local src="$1" dest="$2"
    if command -v rsync >/dev/null 2>&1; then
        rsync -a \
            --exclude '__pycache__' --exclude '.DS_Store' \
            --exclude '.git' --exclude '.gitignore' --exclude '.github' \
            --exclude 'tests' --exclude '*.egg-info' \
            --exclude '.pytest_cache' --exclude '.ruff_cache' --exclude '.venv' \
            "$src"/ "$dest"/
    else
        (cd "$src" && tar \
            --exclude '__pycache__' --exclude '.DS_Store' \
            --exclude '.git' --exclude '.gitignore' --exclude '.github' \
            --exclude 'tests' --exclude '*.egg-info' \
            --exclude '.pytest_cache' --exclude '.ruff_cache' --exclude '.venv' \
            -cf - .) | (cd "$dest" && tar -xf -)
    fi
}

sweep_backups() {             # keep newest $KEEP_BACKUPS brickprint.* dirs
    [[ "$KEEP_BACKUPS" -gt 0 && -d "$BACKUP_ROOT" ]] || return 0
    local idx=0 d
    while IFS= read -r d; do
        [[ -n "$d" && -d "$d" && ! -L "$d" ]] || continue
        idx=$((idx + 1))
        if [[ "$idx" -gt "$KEEP_BACKUPS" ]]; then
            rm -rf "$d"
        fi
    done < <(ls -1dt "${BACKUP_ROOT}"/brickprint.* 2>/dev/null || true)
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

install_one "${HOME}/.gemini/config/skills/brickprint"
install_one "${HOME}/.codex/skills/brickprint"
install_one "${HOME}/.claude/skills/brickprint"
sweep_backups

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
echo "  ${HOME}/.gemini/config/skills/brickprint/bin/brickprint doctor"
echo "  ${HOME}/.claude/skills/brickprint/bin/brickprint doctor"
echo "  ${HOME}/.codex/skills/brickprint/bin/brickprint doctor"
echo "  ${HOME}/.gemini/config/skills/brickprint/bin/brickprint build 10337-1 --bambu --verify"
