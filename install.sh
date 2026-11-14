#!/usr/bin/env bash
# Install brickprint as a global skill for Codex and Claude.
#
#   ./install.sh          copy
#   ./install.sh --link   symlink (development)
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="copy"
if [[ "${1:-}" == "--link" ]]; then
    MODE="link"
fi

CONFIG_DIR="${HOME}/.config/brickprint"
CONFIG_FILE="${CONFIG_DIR}/config.json"
PROJECT="$(cd "$HERE/../.." && pwd)"

project_ldraw=""
project_ldview=""
if [[ -f "$PROJECT/ldraw_library/ldraw/LDConfig.ldr" ]]; then
    project_ldraw="$PROJECT/ldraw_library/ldraw"
fi
if [[ -x "$PROJECT/tools/LDView.app/Contents/MacOS/LDView" ]]; then
    project_ldview="$PROJECT/tools/LDView.app/Contents/MacOS/LDView"
fi

BACKUP_ROOT="${HOME}/Library/Caches/brickprint/backups"

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
        rsync -a --exclude '__pycache__' --exclude '.DS_Store' --exclude '.git' --exclude '.gitignore' \
            "$HERE"/ "$dest"/
        echo "copied $dest"
    fi
    chmod +x "$dest/bin/brickprint" 2>/dev/null || true
}

install_one "${HOME}/.codex/skills/brickprint"
install_one "${HOME}/.claude/skills/brickprint"

mkdir -p "$CONFIG_DIR"
if [[ ! -f "$CONFIG_FILE" ]]; then
    cat > "$CONFIG_FILE" <<EOF
{
 "cache_dir": "${HOME}/Library/Caches/brickprint",
 "ldraw_dir": "${project_ldraw}",
 "ldview": "${project_ldview}",
 "bambu": "/Applications/BambuStudio.app/Contents/MacOS/BambuStudio"
}
EOF
    echo "wrote $CONFIG_FILE"
else
    echo "kept existing $CONFIG_FILE"
fi

echo
echo "Installed.  Try:"
echo "  ${HOME}/.codex/skills/brickprint/bin/brickprint doctor"
echo "  ${HOME}/.codex/skills/brickprint/bin/brickprint inventory 10337-1"
echo "  ${HOME}/.codex/skills/brickprint/bin/brickprint build 10337-1 --bambu"
