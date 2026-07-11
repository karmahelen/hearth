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
# Non-interactive mode (for containers / scripted installs):
#   Set HEARTH_NONINTERACTIVE=1 and the script issues no prompts. All decisions
#   come from environment variables — see the "Non-interactive mode" section
#   below for the full reference. Interactive behavior is unchanged when the
#   variable is unset.
#

# === Configuration ===
REPO_URL="${HEARTH_REPO_URL:-https://github.com/karmahelen/hearth.git}"
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
)

# === Globals ===
INSTALL_DIR=""
HEARTH_DIR=""
TEMP_SOURCE=""
RSYNC_EXCLUDES=()

# === Non-interactive mode ===
#
# When HEARTH_NONINTERACTIVE is set (1/true/yes), the script issues no prompts
# and every decision comes from the environment:
#
#   HEARTH_INSTALL_DIR    Parent dir for Hearth/ (default: current directory).
#                         Created if missing.
#   HEARTH_APPS           Submodules to install FIRST-TIME: 'all', 'none', or a
#                         comma list like 'xnote,xlist' (default: all).
#                         Governs first-time installs only — already-installed
#                         submodules are never removed and follow HEARTH_UPDATE.
#   HEARTH_UPDATE         Update policy (default: always):
#                           always  — sync base + installed submodules every run
#                           missing — install absent components, never touch
#                                     existing ones
#                           never   — if an install exists, touch nothing and
#                                     skip the clone entirely (offline mode);
#                                     error if no install exists
#   HEARTH_REF            Branch or tag to clone (default: repo default branch)
#   HEARTH_REPO_URL       Repo URL override (also honored in interactive mode)
#   HEARTH_ALLOW_OVERLAY  Permit installing into a non-empty directory that has
#                         no hearth.py (the safety prompt's override; default:
#                         abort)
#
# In this mode, missing dependencies (git/rsync/uv) are a hard failure with a
# named message — the script never sudo-installs anything unattended.
NONINTERACTIVE=0
case "${HEARTH_NONINTERACTIVE:-}" in
    1|[Tt][Rr][Uu][Ee]|[Yy]|[Yy][Ee][Ss]) NONINTERACTIVE=1 ;;
esac

HEARTH_UPDATE="${HEARTH_UPDATE:-always}"
HEARTH_APPS="${HEARTH_APPS:-all}"

# Decision narration. With no human confirming each step, the log is the
# audit trail — every policy-driven choice gets an explicit line.
narrate() {
    echo "[hearth-install] $*"
}

# Fail fast on a policy typo instead of silently acting like some default.
if [[ $NONINTERACTIVE -eq 1 ]]; then
    case "$HEARTH_UPDATE" in
        always|missing|never) ;;
        *)
            narrate "Invalid HEARTH_UPDATE='$HEARTH_UPDATE' (expected: always | missing | never). Aborting." >&2
            exit 1
            ;;
    esac
fi

# Does HEARTH_APPS select this submodule name for first-time install?
app_selected() {
    local name="$1"
    case "$HEARTH_APPS" in
        all)     return 0 ;;
        none|"") return 1 ;;
    esac
    local IFS=',' item
    for item in $HEARTH_APPS; do
        item="${item//[[:space:]]/}"
        [[ "$item" == "$name" ]] && return 0
    done
    return 1
}

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
    # Itemize format: update-type char, then file-type char — a changed file
    # is '>fcst......' and a new file is '>f+++++++++', so 'f' is the SECOND
    # character. The previous pattern (^[<>ch].f) required it third and never
    # matched, making change detection silently report "up to date" forever.
    # The .? keeps the match tolerant of either layout. Leading '.' lines
    # (attr-only changes, e.g. mtime noise from fresh clones) stay excluded.
    output=$(rsync -anc --itemize-changes "${RSYNC_EXCLUDES[@]}" "$source/" "$dest/" 2>/dev/null \
        | awk '/^[<>ch].?f/')

    [[ -n "$output" ]]
}

# Show a list of files that would change.
show_changed_files() {
    local source="$1"
    local dest="$2"

    build_rsync_excludes "$source"

    # Same itemize-position fix as has_changes — see the comment there.
    rsync -anc --itemize-changes "${RSYNC_EXCLUDES[@]}" "$source/" "$dest/" 2>/dev/null \
        | awk '/^[<>ch].?f/ {print "  " $NF}' \
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
    if [[ $NONINTERACTIVE -eq 1 ]]; then
        narrate "Missing dependency: git. Install it and re-run. Aborting." >&2
        exit 1
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
    if [[ $NONINTERACTIVE -eq 1 ]]; then
        narrate "Missing dependency: rsync. Install it and re-run. Aborting." >&2
        exit 1
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
    if [[ $NONINTERACTIVE -eq 1 ]]; then
        narrate "Missing dependency: uv. Install it and re-run. Aborting." >&2
        narrate "Official installer: $UV_INSTALL_CMD" >&2
        exit 1
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

    if [[ $NONINTERACTIVE -eq 1 ]]; then
        input="${HEARTH_INSTALL_DIR:-$(pwd)}"
        input="${input/#\~/$HOME}"
        if [[ ! -d "$input" ]]; then
            narrate "Install directory '$input' does not exist — creating it."
            mkdir -p "$input" || { narrate "Failed to create '$input'. Aborting." >&2; exit 1; }
        fi
        if [[ ! -w "$input" ]]; then
            narrate "Directory '$input' is not writable. Aborting." >&2
            exit 1
        fi
        INSTALL_DIR="$input"
        return
    fi

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
    local clone_args=(--depth=1)
    if [[ -n "${HEARTH_REF:-}" ]]; then
        # --branch accepts both branch names and tags
        clone_args+=(--branch "$HEARTH_REF")
        echo "Downloading Hearth source (ref: $HEARTH_REF)..."
    else
        echo "Downloading Hearth source..."
    fi
    if ! git clone "${clone_args[@]}" "$REPO_URL" "$TEMP_SOURCE/hearth" 2>/dev/null; then
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
        if [[ $NONINTERACTIVE -eq 1 ]]; then
            case "${HEARTH_ALLOW_OVERLAY:-}" in
                1|[Tt][Rr][Uu][Ee]|[Yy]|[Yy][Ee][Ss])
                    narrate "HEARTH_ALLOW_OVERLAY set — proceeding with overlay install."
                    ;;
                *)
                    narrate "Refusing to overlay a non-Hearth directory. Set HEARTH_ALLOW_OVERLAY=1 to override. Aborting." >&2
                    exit 1
                    ;;
            esac
        elif ! confirm "Proceed anyway?"; then
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
            if [[ $NONINTERACTIVE -eq 1 ]]; then
                if [[ "$HEARTH_UPDATE" == "always" ]]; then
                    narrate "HEARTH_UPDATE=always → applying update to Hearth base."
                    echo "Files being updated:"
                    show_changed_files "$source_main" "$hearth_dir"
                    sync_component "$source_main" "$hearth_dir" "Hearth base" || exit 1
                    echo "✓ Hearth base updated."
                else
                    narrate "HEARTH_UPDATE=$HEARTH_UPDATE → leaving existing Hearth base untouched (updates are available)."
                fi
            else
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
                if [[ $NONINTERACTIVE -eq 1 ]]; then
                    if [[ "$HEARTH_UPDATE" == "always" ]]; then
                        narrate "HEARTH_UPDATE=always → updating submodule '$name'."
                        echo "Files being updated:"
                        show_changed_files "$source_path" "$install_path"
                        sync_component "$source_path" "$install_path" "$name" || continue
                        echo "✓ $name updated."
                    else
                        narrate "HEARTH_UPDATE=$HEARTH_UPDATE → leaving installed submodule '$name' untouched (updates are available)."
                    fi
                else
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
                fi
            else
                echo "Submodule '$name': up to date."
            fi
        else
            # Not installed - first-time install
            if [[ $NONINTERACTIVE -eq 1 ]]; then
                if app_selected "$name"; then
                    narrate "HEARTH_APPS=$HEARTH_APPS → installing submodule '$name'."
                    if ! git -C "$source_main" submodule update --init "$path" 2>/dev/null; then
                        echo "Failed to fetch $name source. Skipping."
                        continue
                    fi
                    sync_component "$source_path" "$install_path" "$name" || continue
                    echo "✓ $name installed."
                else
                    narrate "HEARTH_APPS=$HEARTH_APPS → skipping submodule '$name' (not selected)."
                fi
            else
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
        fi
    done
}

# === Main ===

main() {
    echo "=== Hearth Installer ==="
    if [[ $NONINTERACTIVE -eq 1 ]]; then
        narrate "Non-interactive mode."
        narrate "  install dir : ${HEARTH_INSTALL_DIR:-$(pwd)}"
        narrate "  update mode : $HEARTH_UPDATE"
        narrate "  apps        : $HEARTH_APPS"
        narrate "  ref         : ${HEARTH_REF:-<default branch>}"
        narrate "  repo        : $REPO_URL"
    fi
    echo

    check_git
    check_rsync
    check_uv

    echo
    get_install_dir
    echo "Install location: $INSTALL_DIR"

    # HEARTH_UPDATE=never with an existing install: touch nothing and skip
    # the clone entirely. This doubles as offline / fast-start mode.
    if [[ $NONINTERACTIVE -eq 1 && "$HEARTH_UPDATE" == "never" ]]; then
        HEARTH_DIR="$INSTALL_DIR/$INSTALL_DIR_NAME"
        if [[ -f "$HEARTH_DIR/hearth.py" ]]; then
            narrate "HEARTH_UPDATE=never and existing install found — skipping fetch entirely."
            echo
            echo "=== Done ==="
            echo "Hearth is at: $HEARTH_DIR"
            echo "To run: cd '$HEARTH_DIR' && uv run hearth.py"
            return 0
        fi
        narrate "HEARTH_UPDATE=never but no existing install at '$HEARTH_DIR'." >&2
        narrate "Nothing to run. Use HEARTH_UPDATE=always or HEARTH_UPDATE=missing to bootstrap. Aborting." >&2
        exit 1
    fi

    prepare_source
    install_or_update_main
    handle_submodules

    echo
    echo "=== Done ==="
    echo "Hearth is at: $HEARTH_DIR"
    echo "To run: cd '$HEARTH_DIR' && uv run hearth.py"
}

main "$@"
