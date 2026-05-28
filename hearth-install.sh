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
# The itemize format is "YX........" where Y (position 1) is the operation
# (>, c, h, < = transfer/create; . = attribute-only; * = message). Matching
# lines that START with an operation char captures real content/structural
# changes while ignoring timestamp-only (".f..t...") diffs from the fresh clone.
# Returns 0 if changes would happen, 1 otherwise.
has_changes() {
    local source="$1"
    local dest="$2"

    build_rsync_excludes "$source"

    rsync -anc --itemize-changes "${RSYNC_EXCLUDES[@]}" "$source/" "$dest/" 2>/dev/null \
        | grep -qE '^[<>ch]'
}

# Show a list of files that would change.
show_changed_files() {
    local source="$1"
    local dest="$2"

    build_rsync_excludes "$source"

    # Match operation lines (see has_changes), then strip the itemize flag
    # field so paths with spaces survive intact.
    rsync -anc --itemize-changes "${RSYNC_EXCLUDES[@]}" "$source/" "$dest/" 2>/dev/null \
        | grep -E '^[<>ch]' \
        | sed -E 's/^[^ ]+ /  /' \
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

# Scan immediate subdirectories of $1 for Hearth installs, identified by the
# presence of BOTH hearth.py and a hearthmonitor/ directory (specific enough to
# avoid matching a stray hearth.py). Prints each matching path on its own line.
scan_for_installs() {
    local parent="$1"
    local sub
    for sub in "$parent"/*/; do
        [[ -d "$sub" ]] || continue
        if [[ -f "${sub}hearth.py" && -d "${sub}hearthmonitor" ]]; then
            echo "${sub%/}"
        fi
    done
}

# Resolve HEARTH_DIR / INSTALL_DIR for a fresh install under $1.
# Empty/new directory -> ask whether to install directly or in a Hearth subfolder.
# Non-empty directory  -> treat as the parent and create a Hearth subfolder.
resolve_fresh_install() {
    local input="$1"
    if [[ ! -d "$input" || -z "$(ls -A "$input" 2>/dev/null)" ]]; then
        echo
        echo "Directory '$input' is empty or does not exist yet."
        echo "  Install directly here:        $input"
        echo "  Or create a Hearth subfolder: $input/$INSTALL_DIR_NAME"
        if confirm "Install Hearth directly into '$input'? (No = create a '$INSTALL_DIR_NAME' subfolder)"; then
            HEARTH_DIR="$input"
            INSTALL_DIR="$(dirname "$input")"
        else
            INSTALL_DIR="$input"
            HEARTH_DIR="$input/$INSTALL_DIR_NAME"
        fi
    else
        INSTALL_DIR="$input"
        HEARTH_DIR="$input/$INSTALL_DIR_NAME"
    fi
}

get_install_dir() {
    local input
    prompt_read input "Enter the directory where Hearth should be installed/updated (files are only installed to the Hearth directory so to uninstall just delete). Leave blank for current directory: "
    if [[ -z "$input" ]]; then
        input="$(pwd)"
    fi
    input="${input/#\~/$HOME}"
    # Strip a trailing slash (but never reduce "/" to empty) so dirname and
    # path comparisons behave predictably.
    if [[ "$input" != "/" ]]; then
        input="${input%/}"
    fi

    # Resolve where Hearth lives (or will be created), in priority order:
    #   1. <input>/hearth.py            -> user pointed at the install dir itself
    #   2. <input>/Hearth/hearth.py     -> parent of a standard installer install
    #   3. <input>/hearth/hearth.py     -> parent of a lowercase (git clone) checkout
    #   4. subdir scan                  -> parent of a custom-named install (e.g. hearth-main)
    #   5. fresh install                -> empty/new (ask) or non-empty (create subfolder)
    if [[ -f "$input/hearth.py" ]]; then
        HEARTH_DIR="$input"
        INSTALL_DIR="$(dirname "$input")"
        echo "Detected existing Hearth installation at: $HEARTH_DIR"
    elif [[ -f "$input/$INSTALL_DIR_NAME/hearth.py" ]]; then
        INSTALL_DIR="$input"
        HEARTH_DIR="$input/$INSTALL_DIR_NAME"
        echo "Detected existing Hearth installation at: $HEARTH_DIR"
    elif [[ -f "$input/hearth/hearth.py" ]]; then
        INSTALL_DIR="$input"
        HEARTH_DIR="$input/hearth"
        echo "Detected existing Hearth installation at: $HEARTH_DIR"
    else
        # No standard-named install found. Scan immediate subdirs for a
        # custom-named install before falling back to a fresh install.
        local scanned
        mapfile -t scanned < <(scan_for_installs "$input")

        if [[ ${#scanned[@]} -eq 1 ]]; then
            echo
            echo "Found a Hearth installation at: ${scanned[0]}"
            if confirm "Update this installation?"; then
                HEARTH_DIR="${scanned[0]}"
                INSTALL_DIR="$input"
            else
                resolve_fresh_install "$input"
            fi
        elif [[ ${#scanned[@]} -gt 1 ]]; then
            echo
            echo "Multiple Hearth installations found under '$input':"
            local i
            for i in "${!scanned[@]}"; do
                echo "  $((i + 1)). ${scanned[i]}"
            done
            echo "  0. None of these (fresh install)"
            local choice
            prompt_read choice "Enter the number to update (0 for fresh install): "
            if [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#scanned[@]} )); then
                HEARTH_DIR="${scanned[$((choice - 1))]}"
                INSTALL_DIR="$input"
            else
                resolve_fresh_install "$input"
            fi
        else
            resolve_fresh_install "$input"
        fi
    fi

    # Ensure the parent directory exists (create for fresh installs if needed).
    if [[ ! -d "$INSTALL_DIR" ]]; then
        if confirm "Directory '$INSTALL_DIR' does not exist. Create it?"; then
            mkdir -p "$INSTALL_DIR" || { echo "Failed to create '$INSTALL_DIR'. Aborting."; exit 1; }
        else
            echo "Aborting."
            exit 1
        fi
    fi

    # Writability check depends on whether we're updating an existing Hearth
    # directory (write into it) or creating one inside the parent (write into parent).
    if [[ -d "$HEARTH_DIR" ]]; then
        if [[ ! -w "$HEARTH_DIR" ]]; then
            echo "Directory '$HEARTH_DIR' is not writable. Aborting."
            exit 1
        fi
    else
        if [[ ! -w "$INSTALL_DIR" ]]; then
            echo "Directory '$INSTALL_DIR' is not writable. Aborting."
            exit 1
        fi
    fi
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

# If $1 is a git checkout, handle it safely before any installer overlay.
# Uses -e (not -d) so it detects BOTH a base repo's .git directory AND a
# submodule's .git file.
#
# Mode ($2, optional):
#   "full"       (default, used for the base): clean tree -> warn + confirm;
#                dirty tree -> abort/discard menu.
#   "dirty-only" (used for apps): clean tree -> proceed silently;
#                dirty tree -> abort/discard menu.
#
# Returns 0 to proceed with the overlay, 1 to NOT proceed. The caller decides
# what "don't proceed" means (the base exits the script; an app skips itself).
check_git_managed() {
    local dir="$1"
    local mode="${2:-full}"

    [[ -e "$dir/.git" ]] || return 0   # not a git checkout (catches submodule .git FILES too)

    # Dirty = uncommitted changes to tracked files (what an overlay would clobber).
    # Untracked files are safe (rsync never removes files absent from the source),
    # so we ignore them. A non-zero exit (changes OR git error) is treated as
    # dirty, erring on the side of caution.
    local dirty=0
    git -C "$dir" diff --quiet HEAD -- 2>/dev/null || dirty=1

    if [[ $dirty -eq 0 ]]; then
        # Clean working tree.
        if [[ "$mode" == "dirty-only" ]]; then
            return 0   # app folder, nothing at risk: proceed quietly
        fi
        echo
        echo "Note: '$dir' is a git repository — Hearth appears to have been obtained here via git."
        echo "This installer updates by overlaying files, which can leave the checkout in a"
        echo "git-modified state. To update a git checkout normally, use 'git pull' instead."
        if confirm "Proceed with an installer-style update anyway?"; then
            return 0
        fi
        echo "Leaving '$dir' unchanged. Use 'git pull' to update this checkout."
        return 1
    fi

    # Dirty working tree (both modes present the menu).
    echo
    echo "WARNING: '$dir' is a git checkout with uncommitted changes to tracked files"
    echo "that an installer update would overwrite."
    echo
    echo "  1. Leave it alone (recommended)."
    echo "       To update while KEEPING your changes, use 'git pull'."
    echo "       To use the installer instead, commit or stash your changes first, then re-run."
    echo "  2. Discard my uncommitted changes and update."
    echo
    local choice
    prompt_read choice "Enter choice [1/2] (default 1): "
    case "$choice" in
        2)
            echo "Discarding uncommitted changes (git reset --hard HEAD)..."
            if git -C "$dir" reset --hard HEAD; then
                return 0
            fi
            echo "Failed to discard changes. Leaving '$dir' unchanged."
            return 1
            ;;
        *)
            echo "Leaving '$dir' unchanged to protect your changes."
            return 1
            ;;
    esac
}

install_or_update_main() {
    # HEARTH_DIR was already resolved by get_install_dir (with smart detection).
    local hearth_dir="$HEARTH_DIR"
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
        check_git_managed "$hearth_dir" || exit 0
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
                    # Protect uncommitted work if this app folder is itself a git
                    # checkout (e.g. from a recursive clone). dirty-only: clean or
                    # non-git apps proceed silently; a dirty app gets the menu.
                    check_git_managed "$install_path" "dirty-only" || continue
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
    echo "To get started: cd '$HEARTH_DIR/hearthmonitor' && uv run hearthmonitor.py"
}

main "$@"
