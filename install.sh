#!/bin/sh
# Installs courier (herdr-courier) as a command with uv, installing uv first if it is missing.
#
#   curl -LsSf https://raw.githubusercontent.com/NaNIsaac/herdr-courier/main/install.sh | sh
#
# Run it again to update. uv's own settings apply, e.g. UV_TOOL_BIN_DIR for where the command goes.
set -eu

cli=courier

if ! command -v uv >/dev/null 2>&1; then
    echo "Installing uv (https://docs.astral.sh/uv/)"
    # The uv installer adds its folder to the shell profiles unless UV_NO_MODIFY_PATH is set.
    curl -LsSf https://astral.sh/uv/install.sh | env -u UV_NO_MODIFY_PATH sh
    # It changes PATH for new shells only.
    for dir in "${UV_INSTALL_DIR:-}" "${UV_UNMANAGED_INSTALL:-}" "${XDG_BIN_HOME:-}" "$HOME/.local/bin"; do
        if [ -n "$dir" ] && [ -x "$dir/uv" ]; then
            PATH="$dir:$PATH"
            break
        fi
    done
    command -v uv >/dev/null 2>&1 || { echo "uv was installed but not found; open a new terminal and run this again." >&2; exit 1; }
fi

# Not on PyPI: the main branch as a source archive, so git is not needed. --reinstall fetches it anew.
uv tool install --reinstall "herdr-courier @ https://github.com/NaNIsaac/herdr-courier/archive/refs/heads/main.tar.gz"
# Puts uv's command folder on PATH in the shell profiles for new terminals; does nothing if it is there.
uv tool update-shell
echo "Installed $cli to $(uv tool dir --bin). Next (in a new terminal): $cli --help"
