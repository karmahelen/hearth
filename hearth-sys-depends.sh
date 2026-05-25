#!/usr/bin/env bash
#
# hearth-sys-depends.sh
# System dependency check for Hearth (Qt/QtWebEngine backend).
#
# Usage:
#   hearth-sys-depends.sh              Check and offer to install missing deps
#   hearth-sys-depends.sh --check-only Report only, exit non-zero if anything missing
#   hearth-sys-depends.sh --help       Show usage
#
# Python deps (Flask, pywebview, PyQt6) are handled by `uv run` with PEP 723
# inline script headers. This script only covers system libraries that uv
# cannot install.
#
# Exit codes:
#   0  All dependencies present, or install completed successfully
#   1  Dependencies missing (--check-only, user declined, or non-apt distro)
#   2  Install failed
#   3  Distribution not supported for automated install
#

set -uo pipefail

readonly SCRIPT_NAME="hearth-sys-depends"

# ──────────────────────────────────────────────────────────────────────────
# Dependency table
#
# Each row: <so_file>|<description>|<apt>|<dnf>|<pacman>|<zypper>
#
# Note on casing: XCB libs and ATK/Pango/etc. are lowercase on disk; the
# Xlib bindings (libXcomposite, libXdamage, libXrandr) and EGL keep their
# uppercase letters. ldconfig -p matches the actual filename, so case matters.
# ──────────────────────────────────────────────────────────────────────────
readonly DEPS=(
    "libxcb-cursor.so.0|XCB cursor (Qt 6.5+ requirement)|libxcb-cursor0|xcb-util-cursor|xcb-util-cursor|libxcb-cursor0"
    "libxcb-icccm.so.4|XCB ICCCM window management|libxcb-icccm4|xcb-util-wm|xcb-util-wm|libxcb-icccm4"
    "libxcb-image.so.0|XCB image utilities|libxcb-image0|xcb-util-image|xcb-util-image|libxcb-image0"
    "libxcb-keysyms.so.1|XCB key symbols|libxcb-keysyms1|xcb-util-keysyms|xcb-util-keysyms|libxcb-keysyms1"
    "libxcb-randr.so.0|XCB RandR|libxcb-randr0|libxcb|libxcb|libxcb-randr0"
    "libxcb-render-util.so.0|XCB render utilities|libxcb-render-util0|xcb-util-renderutil|xcb-util-renderutil|libxcb-render-util0"
    "libxcb-shape.so.0|XCB shape extension|libxcb-shape0|libxcb|libxcb|libxcb-shape0"
    "libxcb-sync.so.1|XCB sync extension|libxcb-sync1|libxcb|libxcb|libxcb-sync1"
    "libxcb-xfixes.so.0|XCB xfixes|libxcb-xfixes0|libxcb|libxcb|libxcb-xfixes0"
    "libxcb-xinerama.so.0|XCB Xinerama|libxcb-xinerama0|libxcb|libxcb|libxcb-xinerama0"
    "libxcb-xkb.so.1|XCB XKB|libxcb-xkb1|libxcb|libxcb|libxcb-xkb1"
    "libxkbcommon-x11.so.0|xkbcommon X11 keyboard|libxkbcommon-x11-0|libxkbcommon-x11|libxkbcommon|libxkbcommon-x11-0"
    "libnss3.so|NSS (TLS for embedded Chromium)|libnss3|nss|nss|mozilla-nss"
    "libnspr4.so|NSPR (Netscape Portable Runtime)|libnspr4|nspr|nss|mozilla-nspr"
    "libasound.so.2|ALSA audio runtime|libasound2|alsa-lib|alsa-lib|libasound2"
    "libcups.so.2|CUPS (Chromium hard-deps this)|libcups2|cups-libs|libcups|libcups2"
    "libdrm.so.2|DRM (direct rendering)|libdrm2|libdrm|libdrm|libdrm2"
    "libgbm.so.1|GBM (graphics buffer management)|libgbm1|mesa-libgbm|mesa|Mesa-libgbm1"
    "libEGL.so.1|EGL graphics|libegl1|mesa-libEGL|mesa|libEGL1"
    "libXcomposite.so.1|X composite|libxcomposite1|libXcomposite|libxcomposite|libXcomposite1"
    "libXdamage.so.1|X damage|libxdamage1|libXdamage|libxdamage|libXdamage1"
    "libXrandr.so.2|X randr|libxrandr2|libXrandr|libxrandr|libXrandr2"
    "libatk-1.0.so.0|ATK accessibility|libatk1.0-0|atk|atk|libatk-1_0-0"
    "libatk-bridge-2.0.so.0|ATK bridge|libatk-bridge2.0-0|at-spi2-atk|at-spi2-atk|libatk-bridge-2_0-0"
    "libpango-1.0.so.0|Pango text rendering|libpango-1.0-0|pango|pango|libpango-1_0-0"
)

# ──────────────────────────────────────────────────────────────────────────
# Output styling (TTY-aware; respects NO_COLOR)
# ──────────────────────────────────────────────────────────────────────────
if [[ -t 1 ]] && [[ "${TERM:-dumb}" != "dumb" ]] && [[ -z "${NO_COLOR:-}" ]]; then
    C_BOLD=$'\033[1m';   C_DIM=$'\033[2m'
    C_RED=$'\033[31m';   C_GREEN=$'\033[32m'
    C_YELLOW=$'\033[33m'; C_CYAN=$'\033[36m'
    C_RESET=$'\033[0m'
    SYM_OK="✓"
    SYM_MISS="✗"
    BOX_TL="╭"; BOX_TR="╮"; BOX_BL="╰"; BOX_BR="╯"
    BOX_H="─"; BOX_V="│"
else
    C_BOLD='';  C_DIM=''; C_RED=''; C_GREEN=''; C_YELLOW=''; C_CYAN=''; C_RESET=''
    SYM_OK="[ok]"
    SYM_MISS="[--]"
    BOX_TL="+"; BOX_TR="+"; BOX_BL="+"; BOX_BR="+"
    BOX_H="-"; BOX_V="|"
fi

# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────

show_help() {
    cat <<EOF
${C_BOLD}${SCRIPT_NAME}${C_RESET} — System dependency check for Hearth (Qt backend)

${C_BOLD}Usage:${C_RESET}
    ${SCRIPT_NAME}.sh [options]

${C_BOLD}Options:${C_RESET}
    --check-only    Report dependency status and exit. No prompts, no install.
                    Exits non-zero if anything is missing. Use in scripts/CI.

    --help, -h      Show this help and exit.

${C_BOLD}Behavior:${C_RESET}
    Detects your Linux distribution and checks for system libraries required
    by Qt + QtWebEngine. On Debian-family systems (apt) it offers to install
    missing packages directly. On other distros it prints the suggested
    install command for you to verify and run yourself.

    Python-level dependencies (Flask, pywebview, PyQt6) are managed by
    'uv run' with PEP 723 inline script headers and are not checked here.

${C_BOLD}Exit codes:${C_RESET}
    0   All dependencies present, or install completed successfully
    1   Dependencies missing (--check-only, declined, or non-apt distro)
    2   Install failed
    3   Distribution not supported for automated install
EOF
}

# Set DISTRO_FAMILY / DISTRO_ID / DISTRO_NAME from /etc/os-release
detect_distro() {
    if [[ ! -r /etc/os-release ]]; then
        DISTRO_FAMILY="unknown"
        DISTRO_ID="unknown"
        DISTRO_NAME="Unknown Linux"
        return
    fi
    # shellcheck disable=SC1091
    . /etc/os-release
    DISTRO_ID="${ID:-unknown}"
    DISTRO_NAME="${PRETTY_NAME:-$DISTRO_ID}"
    local id_like="${ID_LIKE:-}"

    case "$DISTRO_ID" in
        debian|ubuntu|linuxmint|pop|elementary|zorin|kali|raspbian|mxlinux|deepin)
            DISTRO_FAMILY="debian" ;;
        fedora|rhel|centos|rocky|almalinux|ol|amzn)
            DISTRO_FAMILY="fedora" ;;
        arch|manjaro|endeavouros|garuda|cachyos|arcolinux)
            DISTRO_FAMILY="arch" ;;
        opensuse*|sles|sled|suse)
            DISTRO_FAMILY="suse" ;;
        *)
            case "$id_like" in
                *debian*|*ubuntu*) DISTRO_FAMILY="debian" ;;
                *fedora*|*rhel*)   DISTRO_FAMILY="fedora" ;;
                *arch*)            DISTRO_FAMILY="arch" ;;
                *suse*)            DISTRO_FAMILY="suse" ;;
                *)                 DISTRO_FAMILY="unknown" ;;
            esac
            ;;
    esac
}

# Is the given .so file findable via the dynamic linker cache?
#
# Greps a pre-captured copy of `ldconfig -p` (LDCONFIG_CACHE) rather than
# piping ldconfig into grep directly. Reason: with `set -o pipefail`, when
# `grep -q` finds a match it exits early, ldconfig then gets SIGPIPE on its
# next write (exit code 141), and pipefail propagates that as the pipe's
# exit code — producing a false "missing" result for any lib whose entry
# appears past the pipe buffer in ldconfig's output. Using a here-string
# against a variable side-steps the whole pipe-with-early-exit hazard.
so_present() {
    grep -q -F "$1" <<<"$LDCONFIG_CACHE"
}

# Extract the right package name field from a dep entry based on DISTRO_FAMILY.
pkg_for_family() {
    case "$DISTRO_FAMILY" in
        debian)  echo "$1" | cut -d'|' -f3 ;;
        fedora)  echo "$1" | cut -d'|' -f4 ;;
        arch)    echo "$1" | cut -d'|' -f5 ;;
        suse)    echo "$1" | cut -d'|' -f6 ;;
        *)       echo "" ;;
    esac
}

print_header() {
    local title="  Hearth System Dependency Check             "
    local sub="  ${C_DIM}Backend: Qt (QtWebEngine via PyQt6)${C_RESET}        "
    echo
    echo "${C_CYAN}${BOX_TL}$(printf '%.0s'"$BOX_H" {1..46})${BOX_TR}${C_RESET}"
    echo "${C_CYAN}${BOX_V}${C_RESET}${C_BOLD}${title}${C_RESET}${C_CYAN}${BOX_V}${C_RESET}"
    echo "${C_CYAN}${BOX_V}${C_RESET}${sub}${C_CYAN}${BOX_V}${C_RESET}"
    echo "${C_CYAN}${BOX_BL}$(printf '%.0s'"$BOX_H" {1..46})${BOX_BR}${C_RESET}"
    echo
}

print_rule() {
    printf "${C_DIM}"
    printf '%.0s'"$BOX_H" {1..48}
    printf "${C_RESET}\n"
}

# ──────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────

CHECK_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --check-only) CHECK_ONLY=1 ;;
        --help|-h)    show_help; exit 0 ;;
        *)            echo "Unknown option: $arg" >&2
                      echo "Try --help" >&2
                      exit 1 ;;
    esac
done

# Sanity: ldconfig must exist
if ! command -v ldconfig >/dev/null 2>&1; then
    echo "Error: ldconfig not found on PATH. Cannot probe the linker cache." >&2
    exit 3
fi

# Capture the linker cache once so the per-dep check can grep a variable
# instead of repeatedly piping ldconfig's output. See so_present() for
# the full reason — short version: avoids a pipefail+SIGPIPE false-negative
# bug and also saves 24 ldconfig invocations.
LDCONFIG_CACHE="$(ldconfig -p 2>/dev/null)"
if [[ -z "$LDCONFIG_CACHE" ]]; then
    echo "Error: ldconfig -p returned no output. Linker cache may be empty or corrupt." >&2
    exit 3
fi

print_header
detect_distro

echo "${C_BOLD}System:${C_RESET}  $DISTRO_NAME"
case "$DISTRO_FAMILY" in
    debian) echo "${C_BOLD}Family:${C_RESET}  Debian / apt   ${C_GREEN}${SYM_OK} full auto-install supported${C_RESET}" ;;
    fedora) echo "${C_BOLD}Family:${C_RESET}  Fedora / dnf   ${C_YELLOW}install command will be printed, not executed${C_RESET}" ;;
    arch)   echo "${C_BOLD}Family:${C_RESET}  Arch / pacman  ${C_YELLOW}install command will be printed, not executed${C_RESET}" ;;
    suse)   echo "${C_BOLD}Family:${C_RESET}  SUSE / zypper  ${C_YELLOW}install command will be printed, not executed${C_RESET}" ;;
    *)      echo "${C_BOLD}Family:${C_RESET}  Unknown        ${C_YELLOW}missing libs listed by .so name only${C_RESET}" ;;
esac
echo
echo "${C_BOLD}Checking ${#DEPS[@]} dependencies...${C_RESET}"
echo

missing_idx=()
present_count=0
missing_count=0

for i in "${!DEPS[@]}"; do
    entry="${DEPS[$i]}"
    so_file="$(echo "$entry" | cut -d'|' -f1)"
    desc="$(echo "$entry" | cut -d'|' -f2)"

    if so_present "$so_file"; then
        printf "  ${C_GREEN}%s${C_RESET}  %-30s ${C_DIM}%s${C_RESET}\n" "$SYM_OK" "$so_file" "$desc"
        present_count=$((present_count + 1))
    else
        printf "  ${C_RED}%s${C_RESET}  %-30s ${C_RED}%s${C_RESET} ${C_DIM}(%s)${C_RESET}\n" "$SYM_MISS" "$so_file" "missing" "$desc"
        missing_idx+=("$i")
        missing_count=$((missing_count + 1))
    fi
done

echo
print_rule
echo
echo "${C_BOLD}Status:${C_RESET}  ${C_GREEN}${present_count} present${C_RESET}, ${C_RED}${missing_count} missing${C_RESET}"
echo

if (( missing_count == 0 )); then
    echo "${C_GREEN}${C_BOLD}All Qt system dependencies are present. Hearth should run.${C_RESET}"
    echo
    exit 0
fi

# Build the package list for missing deps (deduped)
missing_pkgs=()
for idx in "${missing_idx[@]}"; do
    pkg="$(pkg_for_family "${DEPS[$idx]}")"
    [[ -n "$pkg" ]] && missing_pkgs+=("$pkg")
done
if (( ${#missing_pkgs[@]} > 0 )); then
    mapfile -t missing_pkgs < <(printf '%s\n' "${missing_pkgs[@]}" | awk '!seen[$0]++')
fi

echo "${C_BOLD}Missing dependencies:${C_RESET}"
for idx in "${missing_idx[@]}"; do
    entry="${DEPS[$idx]}"
    so_file="$(echo "$entry" | cut -d'|' -f1)"
    desc="$(echo "$entry" | cut -d'|' -f2)"
    pkg="$(pkg_for_family "$entry")"
    if [[ -n "$pkg" ]]; then
        printf "  ${C_RED}•${C_RESET} %-28s → ${C_CYAN}%s${C_RESET}\n" "$so_file" "$pkg"
    else
        printf "  ${C_RED}•${C_RESET} %-28s ${C_DIM}(no package mapping for this distro)${C_RESET}\n" "$so_file"
    fi
    printf "    ${C_DIM}%s${C_RESET}\n" "$desc"
done
echo

# --check-only: report and bail
if (( CHECK_ONLY == 1 )); then
    echo "${C_YELLOW}${C_BOLD}--check-only specified — not running any installer.${C_RESET}"
    echo
    exit 1
fi

# Branch on distro for install action
case "$DISTRO_FAMILY" in
    debian)
        if (( ${#missing_pkgs[@]} == 0 )); then
            echo "${C_YELLOW}No apt packages mapped for the missing libs — cannot proceed.${C_RESET}"
            exit 3
        fi
        echo "${C_BOLD}Proposed action:${C_RESET}"
        echo "  ${C_CYAN}\$ sudo apt update${C_RESET}"
        echo "  ${C_CYAN}\$ sudo apt install -y ${missing_pkgs[*]}${C_RESET}"
        echo
        echo "${C_DIM}This will require your sudo password.${C_RESET}"
        read -r -p "Proceed? [y/N] " reply
        case "$reply" in
            [yY]|[yY][eE][sS])
                echo
                echo "${C_BOLD}Updating package index...${C_RESET}"
                if ! sudo apt update; then
                    echo "${C_YELLOW}apt update reported errors — continuing anyway.${C_RESET}"
                fi
                echo
                echo "${C_BOLD}Installing packages...${C_RESET}"
                if sudo apt install -y "${missing_pkgs[@]}"; then
                    echo
                    echo "${C_GREEN}${C_BOLD}Done. Re-run this script to verify.${C_RESET}"
                    echo
                    exit 0
                else
                    echo
                    echo "${C_RED}${C_BOLD}Install failed.${C_RESET}"
                    echo
                    exit 2
                fi
                ;;
            *)
                echo
                echo "${C_YELLOW}Skipped. Hearth may not run until these are installed.${C_RESET}"
                echo
                exit 1
                ;;
        esac
        ;;
    fedora)
        echo "${C_BOLD}Suggested install command (verify package names for your distro):${C_RESET}"
        echo "  ${C_CYAN}\$ sudo dnf install ${missing_pkgs[*]}${C_RESET}"
        echo
        exit 1
        ;;
    arch)
        echo "${C_BOLD}Suggested install command (verify package names for your distro):${C_RESET}"
        echo "  ${C_CYAN}\$ sudo pacman -S ${missing_pkgs[*]}${C_RESET}"
        echo
        exit 1
        ;;
    suse)
        echo "${C_BOLD}Suggested install command (verify package names for your distro):${C_RESET}"
        echo "  ${C_CYAN}\$ sudo zypper install ${missing_pkgs[*]}${C_RESET}"
        echo
        exit 1
        ;;
    *)
        echo "${C_YELLOW}${C_BOLD}Distribution not recognized.${C_RESET}"
        echo "Use your package manager to install libraries providing the missing"
        echo ".so files listed above."
        echo
        exit 3
        ;;
esac
