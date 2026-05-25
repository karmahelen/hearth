#!/bin/bash
#
# hearth-install.sh
#
# Installer for Hearth and its submodule apps.
# Repo: https://github.com/karmahelen/hearth
#
# Usage (one-liner):
#   curl -fsSL https://raw.githubusercontent.com/karmahelen/hearth/main/hearth-install.sh | bash
#
# Usage (manual):
#   bash hearth-install.sh
#
# How it works:
#   1. Clones the repo to a temporary directory (NOT the install location).
#   2. Uses rsync to copy code files from the temp clone to the install directory,
#      excluding git metadata and user data files (per the repo's .gitignore).
#   3. Deletes the temp clone. The install directory ends up with code files only —
#      no .git, no .github, no LICENSE/README/.gitignore/.gitmodules.
#   4. On re-run, the same flow detects which files have changed and offers to update.
#

# === Configuration ===
REPO_URL="https://github.com/karmahelen/hearth.git"
INSTALL_DIR_NAME="Hearth"
UV_INSTALL_CMD='curl -LsSf https://astral.sh/uv/install.sh | sh'

# Files/dirs from the repo that should NEVER be copied to the install location
METADATA_EXCLUDES=(
    ".git"
    ".github"
    "LICENSE"
    "README.md"
    ".gitignore"
    ".gitmodules"
    "hearth-install.sh"
    "hearth-sys-depends.sh"
    "AppPics"
    "AppPics.html"
)

# === Globals ===
INSTALL_DIR=""
HEARTH_DIR=""
TEMP_SOURCE=""
RSYNC_EXCLUDES=()

# === Cleanup on exit (always runs, even on Ctrl-C or errors) ===
cleanup() {
    if [[ -n "$TEMP_SOURCE" && -d "$TEMP_SOURCE" ]]; then
        rm -rf "$TEMP_SOURCE"
    fi
}
trap cleanup EXIT

# === Helpers ===

# Read input from /dev/tty so this works when run via `curl | bash`
prompt_read() {
    local var_name="$1"
    local prompt_text="$2"
    read -r -p "$prompt_text" "$var_name" < /dev/tty
}

confirm() {
    local prompt="$1"
    local response
    prompt_read response "$prompt (y/N) "
    [[ "$response" =~ ^[Yy]$ ]]
}

# Build rsync exclude args for a given source directory.
# Combines hardcoded metadata excludes with the source's .gitignore patterns.
# Sets the global RSYNC_EXCLUDES array.
build_rsync_excludes() {
    local source_dir="$1"
    RSYNC_EXCLUDES=()

    for item in "${METADATA_EXCLUDES[@]}"; do
        RSYNC_EXCLUDES+=("--exclude=$item")
    done

    if [[ -f "$source_dir/.gitignore" ]]; then
        RSYNC_EXCLUDES+=("--exclude-from=$source_dir/.gitignore")
    fi
}

# Detect whether rsync would change anything between source and dest.
# Uses checksum-based comparison (-c) since fresh clones have current mtimes.
# Returns 0 if changes would happen, 1 otherwise.
has_changes() {
    local source="$1"
    local dest="$2"

    build_rsync_excludes "$source"

    local output
    output=$(rsync -anc --itemize-changes "${RSYNC_EXCLUDES[@]}" "$source/" "$dest/" 2>/dev/null \
        | awk '/^[<>ch].f/')

    [[ -n "$output" ]]
}

# Show a list of files that would change.
show_changed_files() {
    local source="$1"
    local dest="$2"

    build_rsync_excludes "$source"

    rsync -anc --itemize-changes "${RSYNC_EXCLUDES[@]}" "$source/" "$dest/" 2>/dev/null \
        | awk '/^[<>ch].f/ {print "  " $NF}' \
        | head -30
}

# Copy code files from source to dest, preserving user data files in dest.
# Returns 0 on success, 1 on failure.
sync_component() {
    local source="$1"
    local dest="$2"
    local label="$3"

    mkdir -p "$dest"
    build_rsync_excludes "$source"

    if rsync -a "${RSYNC_EXCLUDES[@]}" "$source/" "$dest/"; then
        return 0
    else
        echo "Failed to sync $label."
        return 1
    fi
}

# === Dependency checks ===

check_git() {
    if command -v git >/dev/null 2>&1; then
        echo "✓ git is installed."
        return 0
    fi
    echo "git is not installed. It is required to download Hearth."
    if confirm "Install git via 'sudo apt install git'?"; then
        sudo apt install -y git || { echo "Failed to install git. Aborting."; exit 1; }
        echo "✓ git installed."
    else
        echo "Cannot proceed without git. Aborting."
        exit 1
    fi
}

check_rsync() {
    if command -v rsync >/dev/null 2>&1; then
        echo "✓ rsync is installed."
        return 0
    fi
    echo "rsync is not installed. It is required to copy Hearth files."
    if confirm "Install rsync via 'sudo apt install rsync'?"; then
        sudo apt install -y rsync || { echo "Failed to install rsync. Aborting."; exit 1; }
        echo "✓ rsync installed."
    else
        echo "Cannot proceed without rsync. Aborting."
        exit 1
    fi
}

check_uv() {
    if command -v uv >/dev/null 2>&1; then
        echo "✓ uv is installed."
        return 0
    fi
    echo "uv (Python package manager from Astral) is not installed."
    echo "It is used to run Hearth's apps. The official installer command is:"
    echo "  $UV_INSTALL_CMD"
    if confirm "Install uv via the official Astral installer?"; then
        eval "$UV_INSTALL_CMD"
        if [[ -f "$HOME/.local/bin/env" ]]; then
            # shellcheck disable=SC1091
            source "$HOME/.local/bin/env"
        fi
        if command -v uv >/dev/null 2>&1; then
            echo "✓ uv installed and available."
        else
            echo "uv installed but not yet in PATH. Open a new terminal before running Hearth,"
            echo "or run: source ~/.local/bin/env"
        fi
    else
        echo "User will need to manage the necessary python packages manually."
    fi
}

# === Install directory selection ===

get_install_dir() {
    local input
    prompt_read input "Enter the directory to install Hearth (leave blank for current directory): "
    if [[ -z "$input" ]]; then
        input="$(pwd)"
    fi
    input="${input/#\~/$HOME}"

    if [[ ! -d "$input" ]]; then
        if confirm "Directory '$input' does not exist. Create it?"; then
            mkdir -p "$input" || { echo "Failed to create '$input'. Aborting."; exit 1; }
        else
            echo "Aborting."
            exit 1
        fi
    fi

    if [[ ! -w "$input" ]]; then
        echo "Directory '$input' is not writable. Aborting."
        exit 1
    fi

    INSTALL_DIR="$input"
}

# === Source preparation (temp clone) ===

prepare_source() {
    TEMP_SOURCE=$(mktemp -d)
    echo "Downloading Hearth source..."
    if ! git clone --depth=1 "$REPO_URL" "$TEMP_SOURCE/hearth" 2>/dev/null; then
        echo "Failed to download Hearth source. Aborting."
        exit 1
    fi
}

# === Install / update flow ===

install_or_update_main() {
    local hearth_dir="$INSTALL_DIR/$INSTALL_DIR_NAME"
    HEARTH_DIR="$hearth_dir"
    local source_main="$TEMP_SOURCE/hearth"

    # Sanity check: directory exists and has content but no hearth.py
    if [[ -d "$hearth_dir" && -n "$(ls -A "$hearth_dir" 2>/dev/null)" && ! -f "$hearth_dir/hearth.py" ]]; then
        echo
        echo "Warning: '$hearth_dir' exists and has content, but no hearth.py was found."
        echo "Installing here will overlay Hearth files onto existing content."
        if ! confirm "Proceed anyway?"; then
            echo "Aborting."
            exit 1
        fi
    fi

    # Treat as existing install if hearth.py is present
    local is_existing=0
    if [[ -f "$hearth_dir/hearth.py" ]]; then
        is_existing=1
    fi

    echo
    if [[ $is_existing -eq 0 ]]; then
        echo "Installing Hearth base to '$hearth_dir'..."
        sync_component "$source_main" "$hearth_dir" "Hearth base" || exit 1
        echo "✓ Hearth base installed."
    else
        echo "Existing Hearth install found at '$hearth_dir'."
        echo "Checking for updates..."

        if has_changes "$source_main" "$hearth_dir"; then
            echo "Updates available for the Hearth base."
            echo "Files that will be updated:"
            show_changed_files "$source_main" "$hearth_dir"
            echo
            echo "NOTE: User data files (gitignored) are preserved. Any local modifications"
            echo "to tracked code files will be overwritten."
            echo
            if confirm "Apply update?"; then
                sync_component "$source_main" "$hearth_dir" "Hearth base" || exit 1
                echo "✓ Hearth base updated."
            else
                echo "Skipping Hearth base update."
            fi
        else
            echo "✓ Hearth base is up to date."
        fi
    fi
}

handle_submodules() {
    local source_main="$TEMP_SOURCE/hearth"

    if [[ ! -f "$source_main/.gitmodules" ]]; then
        return 0
    fi

    local submodule_paths
    mapfile -t submodule_paths < <(
        git -C "$source_main" config --file .gitmodules --get-regexp 'submodule\..*\.path' | awk '{print $2}'
    )

    if [[ ${#submodule_paths[@]} -eq 0 ]]; then
        return 0
    fi

    echo
    echo "--- Submodules ---"

    for path in "${submodule_paths[@]}"; do
        local name
        name=$(basename "$path")
        local install_path="$HEARTH_DIR/$path"
        local source_path="$source_main/$path"

        # Is this submodule installed locally?
        local is_installed=0
        if [[ -d "$install_path" && -n "$(ls -A "$install_path" 2>/dev/null)" ]]; then
            is_installed=1
        fi

        if [[ $is_installed -eq 1 ]]; then
            # Initialize submodule in temp clone so we can compare
            if ! git -C "$source_main" submodule update --init "$path" 2>/dev/null; then
                echo "Submodule '$name': could not fetch latest source (skipping)."
                continue
            fi

            if has_changes "$source_path" "$install_path"; then
                echo
                echo "Submodule '$name': has updates available."
                echo "Files that will be updated:"
                show_changed_files "$source_path" "$install_path"
                echo
                echo "NOTE: User data files are preserved. Local code modifications will be overwritten."
                echo
                if confirm "Update '$name'?"; then
                    sync_component "$source_path" "$install_path" "$name" || continue
                    echo "✓ $name updated."
                else
                    echo "Skipping $name update."
                fi
            else
                echo "Submodule '$name': up to date."
            fi
        else
            # Not installed - offer first-time install
            echo
            if confirm "Install submodule '$name'?"; then
                if ! git -C "$source_main" submodule update --init "$path" 2>/dev/null; then
                    echo "Failed to fetch $name source. Skipping."
                    continue
                fi
                sync_component "$source_path" "$install_path" "$name" || continue
                echo "✓ $name installed."
            fi
        fi
    done
}

# === Main ===

main() {
    echo "=== Hearth Installer ==="
    echo

    check_git
    check_rsync
    check_uv

    echo
    get_install_dir
    echo "Install location: $INSTALL_DIR"

    prepare_source
    install_or_update_main
    handle_submodules

    echo
    echo "=== Done ==="
    echo "Hearth is at: $HEARTH_DIR"
    echo "To run: cd '$HEARTH_DIR' && uv run hearth.py"
}

main "$@"
