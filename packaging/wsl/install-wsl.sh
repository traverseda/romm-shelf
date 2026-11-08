#!/usr/bin/env bash
# Install romm-shelf inside a WSL2 distribution (docs/wsl.md).
#
#   install-wsl.sh --plan     print every change; change nothing
#   install-wsl.sh            install or update the AppImage and its WSL prerequisites
#   install-wsl.sh --verify   check an existing install; change nothing
#   install-wsl.sh --finish   after 'wsl --shutdown': set linger and verify
#   install-wsl.sh --uninstall
#
# Run as the WSL user, not root: system steps use sudo. The Windows entry point is
# install-wsl.bat, which runs the two phases around the WSL restart systemd needs.

set -euo pipefail

REPO_SLUG="traverseda/romm-shelf"
REPO_URL="https://github.com/${REPO_SLUG}"
MANIFEST_URL="${ROMM_SHELF_MANIFEST_URL:-${REPO_URL}/releases/latest/download/latest.json}"
OS_RELEASE="${ROMM_SHELF_WSL_OS_RELEASE:-/etc/os-release}"
DEST="${ROMM_SHELF_APPIMAGE_DEST:-${HOME}/Applications/romm-shelf.AppImage}"
TARGET_USER="${SUDO_USER:-$(id -un)}"

PLAN=0
VERIFY=0
FINISH=0
UNINSTALL=0
FORCE=0
NO_USERNS=0
SKIP_GPU=0
LOCAL_APPIMAGE=""
VERSION=""
URL=""
SHA256=""
PACKS=""

usage() {
  cat <<'EOF'
Install romm-shelf inside WSL2 (docs/wsl.md).

  install-wsl.sh [--plan] [OPTIONS]

  --plan               print every change; change nothing
  --appimage FILE      install a local AppImage instead of the latest release
  --version V          install release v<V>
  --url URL --sha256 H install an AppImage from URL, verifying H
  --manifest-url URL   where latest.json comes from
  --dest FILE          where the AppImage goes (default ~/Applications/romm-shelf.AppImage)
  --packs NAMES        comma-separated emulator packs (e.g. retroarch,dolphin)
  --no-userns          do not relax Ubuntu's unprivileged-user-namespace restriction
  --skip-gpu-check     skip the OpenGL/Vulkan report
  --verify             check an existing install; change nothing
  --finish             after 'wsl --shutdown': set linger and verify
  --uninstall          remove the AppImage and its launcher; keep config, saves and ROMs
  --force              allow a non-WSL Debian/Ubuntu host
  --os-release FILE    read this instead of /etc/os-release (for tests)
EOF
}

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
warn() { printf 'warning: %s\n' "$*" >&2; }
have() { command -v "$1" >/dev/null 2>&1; }

is_wsl() {
  grep -qiE 'microsoft|wsl' /proc/sys/kernel/osrelease 2>/dev/null || [ -e /dev/dxg ]
}

# run_root is run with sudo (or directly when already root).
run_root() {
  if (( PLAN )); then
    printf '  + sudo'
    printf ' %q' "$@"
    printf '\n'
  elif [ "$(id -u)" -eq 0 ]; then
    "$@"
  else
    sudo "$@"
  fi
}

write_root() {
  local file=$1 content=$2
  if (( PLAN )); then
    printf '  + write %s:\n' "$file"
    printf '%s\n' "$content" | sed 's/^/      /'
  elif [ "$(id -u)" -eq 0 ]; then
    printf '%s\n' "$content" >"$file"
  else
    printf '%s\n' "$content" | sudo tee "$file" >/dev/null
  fi
}

while (( $# )); do
  case "$1" in
    --plan) PLAN=1 ;;
    --verify) VERIFY=1 ;;
    --finish) FINISH=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --force) FORCE=1 ;;
    --no-userns) NO_USERNS=1 ;;
    --skip-gpu-check) SKIP_GPU=1 ;;
    --appimage) LOCAL_APPIMAGE=${2:?--appimage needs a file}; shift ;;
    --version) VERSION=${2:?--version needs a version}; shift ;;
    --url) URL=${2:?--url needs a URL}; shift ;;
    --sha256) SHA256=${2:?--sha256 needs a hash}; shift ;;
    --manifest-url) MANIFEST_URL=${2:?--manifest-url needs a URL}; shift ;;
    --dest) DEST=${2:?--dest needs a path}; shift ;;
    --os-release) OS_RELEASE=${2:?--os-release needs a file}; shift ;;
    --target-user) TARGET_USER=${2:?--target-user needs a user}; shift ;;
    --packs) PACKS="${PACKS:+$PACKS,}${2:?--packs needs a name}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
  shift
done

# The Windows batch passes --appimage/--dest through unchanged. Translate a Windows
# path here so `install-wsl.bat --appimage result\....AppImage` works.
win_to_linux() {
  case "$1" in
    [A-Za-z]:[\\/]*|[A-Za-z]:\\*)
      if have wslpath; then wslpath -a -u "$1"; else printf '%s' "$1"; fi ;;
    *) printf '%s' "$1" ;;
  esac
}
LOCAL_APPIMAGE=$(win_to_linux "$LOCAL_APPIMAGE")
DEST=$(win_to_linux "$DEST")

gpu_report() {
  printf '  gpu:\n'
  local d
  for d in /dev/dxg /dev/dri/card0 /dev/dri/renderD128; do
    if [ -e "$d" ]; then
      printf '    %-18s yes\n' "$d:"
    else
      printf '    %-18s no\n' "$d:"
    fi
  done
  if have glxinfo; then
    local r
    r=$(timeout 10 glxinfo -B 2>/dev/null | sed -n 's/^OpenGL renderer string: //p' | head -n1 || true)
    if [ -n "$r" ]; then
      printf '    %-18s %s\n' "OpenGL:" "$r"
      case "$r" in
        *llvmpipe*|*softpipe*)
          printf '    warning: software rendering; install the Windows GPU driver with WSL support\n' ;;
      esac
    else
      printf '    %-18s not available\n' "OpenGL:"
    fi
  else
    printf '    %-18s glxinfo not installed\n' "OpenGL:"
  fi
  if have vulkaninfo; then
    local v
    v=$(timeout 10 vulkaninfo --summary 2>/dev/null \
        | sed -n 's/^[[:space:]]*deviceName[[:space:]]*=[[:space:]]*//p' | head -n1 || true)
    if [ -n "$v" ]; then
      printf '    %-18s %s\n' "Vulkan:" "$v"
    else
      printf '    %-18s no device\n' "Vulkan:"
      printf '    warning: the bundled emulators default to Vulkan; update WSL and the Windows GPU\n'
      printf '             driver, or use an OpenGL core until WSL exposes a Vulkan device\n'
    fi
  else
    printf '    %-18s vulkaninfo not installed\n' "Vulkan:"
  fi
}

verify() {
  local rc=0
  printf 'romm-shelf in WSL\n'
  printf '  %-10s %s\n' "appimage:" "$DEST"
  if [ -x "$DEST" ]; then
    if "$DEST" --help >/dev/null 2>&1; then
      printf '  %-10s yes\n' "runs:"
    else
      printf '  %-10s NO\n' "runs:"
      rc=1
    fi
  else
    printf '  %-10s NO (not installed)\n' "runs:"
    rc=1
  fi
  if [ -d /run/systemd/system ]; then
    local state
    state=$(systemctl --user is-system-running 2>/dev/null || true)
    case "$state" in
      running|degraded) printf '  %-10s user manager %s\n' "systemd:" "$state" ;;
      *) printf '  %-10s NO user manager (wsl --shutdown, then install-wsl.sh --finish)\n' "systemd:" ;;
    esac
    printf '  %-10s %s\n' "linger:" \
      "$(loginctl show-user "$TARGET_USER" -p Linger --value 2>/dev/null || printf 'unknown')"
  else
    printf '  %-10s not running (set [boot] systemd=true in /etc/wsl.conf, then wsl --shutdown)\n' "systemd:"
  fi
  if (( SKIP_GPU )); then :; else gpu_report; fi
  return $rc
}

finish() {
  if [ -d /run/systemd/system ]; then
    local linger
    linger=$(loginctl show-user "$TARGET_USER" -p Linger --value 2>/dev/null || printf 'no')
    if [ "$linger" != yes ]; then
      run_root loginctl enable-linger "$TARGET_USER"
    fi
  else
    warn "systemd is not running; set [boot] systemd=true in /etc/wsl.conf and wsl --shutdown"
  fi
  verify
}

uninstall() {
  if (( PLAN )); then
    printf '  + rm -f %q %q %q\n' "$DEST" "${DEST}.previous" "${HOME}/.local/bin/romm-shelf"
  else
    rm -f -- "$DEST" "${DEST}.previous" "${HOME}/.local/bin/romm-shelf"
  fi
  printf '# kept: %s\n' "${HOME}/.config/romm-shelf"
  printf '# kept: %s\n' "${XDG_DATA_HOME:-${HOME}/.local/share}/romm-shelf/packs"
  printf '# kept: %s\n' "${ROMM_SHELF_ROOT:-${HOME}/Emulation/romm-shelf}"
  printf '# remove patches first if any are applied: romm-shelf patches remove <NAME>\n'
}

[ -r "$OS_RELEASE" ] || die "cannot read $OS_RELEASE"
os_id=$(sed -n 's/^ID=//p' "$OS_RELEASE" | head -n1 | tr -d '"')
os_ver=$(sed -n 's/^VERSION_ID=//p' "$OS_RELEASE" | head -n1 | tr -d '"')
case "$os_id" in
  ubuntu|debian) ;;
  *) die "unsupported distribution '${os_id:-unknown}': this installer supports Ubuntu and Debian" ;;
esac

arch=$(uname -m)
[ "$arch" = x86_64 ] || die "the AppImage is x86_64 only (this machine is $arch)"

if ! is_wsl; then
  if (( PLAN )); then
    warn "not running under WSL2; printing the plan anyway"
  elif (( FORCE )); then
    warn "not running under WSL2; continuing because of --force"
  else
    die "not running under WSL2 (use --plan to preview, or --force on Debian/Ubuntu)"
  fi
fi

if [ "$(id -u)" -eq 0 ] && (( ! PLAN && ! VERIFY && ! FINISH && ! UNINSTALL )); then
  die "run as your WSL user, not root: system steps use sudo"
fi

if (( UNINSTALL )); then
  uninstall
  exit 0
fi
if (( VERIFY )); then
  verify
  exit
fi
if (( FINISH )); then
  finish
  exit
fi

if (( PLAN )); then
  cat <<EOF
WSL install plan (changes nothing)
  distribution: ${os_id} ${os_ver}
  architecture: ${arch}
  user:         ${TARGET_USER}
  destination:  ${DEST}
EOF
fi

# libfuse2 (the AppImage runtime) and libasound2 gained t64 names on Ubuntu 24.04
# and Debian 13.
major=${os_ver%%.*}
case "$major" in ''|*[!0-9]*) major=0 ;; esac
pkg_fuse=libfuse2
pkg_asound=libasound2
if { [ "$os_id" = ubuntu ] && [ "$major" -ge 24 ]; } \
   || { [ "$os_id" = debian ] && [ "$major" -ge 13 ]; }; then
  pkg_fuse=libfuse2t64
  pkg_asound=libasound2t64
fi
packages=(
  ca-certificates curl python3 xz-utils kmod procps
  fuse3 "$pkg_fuse" bubblewrap
  mesa-utils vulkan-tools mesa-vulkan-drivers
  libgl1 libegl1 libgl1-mesa-dri libegl-mesa0 libglx-mesa0
  "$pkg_asound" libpulse0 xdg-utils fonts-dejavu-core
)

run_root apt-get update
run_root env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  "${packages[@]}"

# systemd is what runs the background service and the patches' user units.
systemd_enabled=0
if grep -Eq '^[[:space:]]*systemd[[:space:]]*=[[:space:]]*true([[:space:]]|$)' /etc/wsl.conf 2>/dev/null; then
  systemd_enabled=1
fi
if [ "$systemd_enabled" -eq 0 ]; then
  if (( PLAN )); then
    printf '  + set [boot] systemd=true in /etc/wsl.conf\n'
  else
    tmp=$(mktemp)
    if [ -f /etc/wsl.conf ]; then
      awk '
        /^[[:space:]]*\[boot\][[:space:]]*$/ { print; print "systemd=true"; seen=1; next }
        { print }
        END { if (!seen) { print ""; print "[boot]"; print "systemd=true" } }
      ' /etc/wsl.conf >"$tmp"
    else
      printf '[boot]\nsystemd=true\n' >"$tmp"
    fi
    if [ "$(id -u)" -eq 0 ]; then
      install -m 0644 "$tmp" /etc/wsl.conf
    else
      sudo install -m 0644 "$tmp" /etc/wsl.conf
    fi
    rm -f "$tmp"
    warn "enabled systemd in /etc/wsl.conf; run 'wsl --shutdown' in Windows, then 'install-wsl.sh --finish'"
  fi
fi

if [ ! -f /etc/modules-load.d/romm-shelf.conf ]; then
  write_root /etc/modules-load.d/romm-shelf.conf "vgem
fuse"
fi
run_root modprobe vgem fuse || warn "could not load vgem/fuse now; they are set to load at boot"

add_groups=""
for g in render video; do
  if getent group "$g" >/dev/null 2>&1; then
    add_groups="${add_groups:+${add_groups},}${g}"
  fi
done
if [ -n "$add_groups" ]; then
  run_root usermod -aG "$add_groups" "$TARGET_USER"
fi

if [ -d /run/systemd/system ]; then
  linger=$(loginctl show-user "$TARGET_USER" -p Linger --value 2>/dev/null || printf 'no')
  if [ "$linger" != yes ]; then
    run_root loginctl enable-linger "$TARGET_USER"
  fi
else
  warn "systemd is not running yet; after 'wsl --shutdown' run 'install-wsl.sh --finish'"
fi

# Ubuntu 24.04 restricts unprivileged user namespaces with AppArmor. bwrap (packs,
# native and Proton games) needs one. The restriction is distribution-wide, so
# this is a deliberate, documented change (todo.md).
if (( NO_USERNS )); then
  warn "--no-userns: pack-backed and native games may fail to start"
else
  userns_conf=""
  apply_restrict=0
  apply_clone=0
  restrict=$(sysctl -n kernel.apparmor_restrict_unprivileged_userns 2>/dev/null || true)
  clone=$(sysctl -n kernel.unprivileged_userns_clone 2>/dev/null || true)
  if [ "$restrict" = 1 ]; then
    warn "Ubuntu restricts unprivileged user namespaces; bubblewrap (packs, native games) needs them"
    userns_conf="${userns_conf}kernel.apparmor_restrict_unprivileged_userns=0"$'\n'
    apply_restrict=1
  fi
  if [ "$clone" = 0 ]; then
    warn "unprivileged user namespaces are disabled; bubblewrap (packs, native games) needs them"
    userns_conf="${userns_conf}kernel.unprivileged_userns_clone=1"$'\n'
    apply_clone=1
  fi
  if [ -n "$userns_conf" ]; then
    write_root /etc/sysctl.d/99-romm-shelf-userns.conf "${userns_conf%$'\n'}"
  fi
  if [ "$apply_restrict" = 1 ]; then
    run_root sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
  fi
  if [ "$apply_clone" = 1 ]; then
    run_root sysctl -w kernel.unprivileged_userns_clone=1
  fi
fi

# Resolve the artifact. In plan mode this only prints where it comes from: no
# network, no download.
local_src=""
if [ -n "$LOCAL_APPIMAGE" ]; then
  local_src=$(realpath -- "$LOCAL_APPIMAGE" 2>/dev/null || printf '%s' "$LOCAL_APPIMAGE")
  if [ -f "$local_src" ]; then
    SHA256=$(sha256sum "$local_src" | awk '{print $1}')
  elif (( PLAN )); then
    warn "no AppImage at $LOCAL_APPIMAGE yet; the plan assumes it will exist"
  else
    die "no AppImage at $LOCAL_APPIMAGE"
  fi
  URL=$local_src
elif [ -n "$URL" ]; then
  [ -n "$SHA256" ] || die "--url needs --sha256"
elif [ -n "$VERSION" ]; then
  URL="${REPO_URL}/releases/download/v${VERSION}/romm-shelf-${VERSION}-x86_64.AppImage"
  if (( PLAN )); then
    printf '  %-23s %s\n' "appimage:" "$URL"
    printf '  %-23s from %s/releases/download/v%s/SHA256SUMS\n' "sha256:" "$REPO_URL" "$VERSION"
    SHA256="<from SHA256SUMS>"
  else
    sums=$(mktemp)
    curl -fsSL --retry 3 -o "$sums" "${REPO_URL}/releases/download/v${VERSION}/SHA256SUMS"
    SHA256=$(awk -v f="$(basename "$URL")" '$2 == f || $2 == "./" f { print $1; exit }' "$sums")
    rm -f "$sums"
    [ -n "$SHA256" ] || die "SHA256SUMS for v${VERSION} has no $(basename "$URL")"
  fi
else
  if (( PLAN )); then
    printf '  %-23s named by %s\n' "appimage:" "$MANIFEST_URL"
  else
    json=$(mktemp)
    curl -fsSL --retry 3 -o "$json" "$MANIFEST_URL" \
      || die "could not fetch ${MANIFEST_URL} (no release yet? use --appimage)"
    URL=$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("appimage", {}).get("url", ""))' <"$json")
    SHA256=$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("appimage", {}).get("sha256", ""))' <"$json")
    released=$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("version", ""))' <"$json")
    rm -f "$json"
    [ -n "$URL" ] && [ -n "$SHA256" ] \
      || die "latest.json has no appimage url/sha256 (no release yet? use --appimage)"
    printf 'release: %s\n' "${released:-unknown}"
  fi
fi

installed_sha=""
if [ -f "$DEST" ]; then
  installed_sha=$(sha256sum "$DEST" | awk '{print $1}')
fi

if (( PLAN )); then
  if [ -n "$installed_sha" ] && [ -n "$SHA256" ] \
     && [ "$SHA256" != "<from SHA256SUMS>" ] && [ "$installed_sha" = "$SHA256" ]; then
    printf '  + %s is already installed\n' "$DEST"
  else
    printf '  + install the AppImage at %s\n' "$DEST"
  fi
  printf '  + ln -sfn %q %q\n' "$DEST" "${HOME}/.local/bin/romm-shelf"
  if ! printf '%s' ":$PATH:" | grep -q ":${HOME}/.local/bin:"; then
    printf '  + add ~/.local/bin to PATH in ~/.profile\n'
  fi
  if [ -n "$PACKS" ]; then
    IFS=',' read -r -a pack_names <<<"$PACKS"
    for name in "${pack_names[@]}"; do
      [ -n "$name" ] || continue
      printf '  + %s packs install %s\n' "$DEST" "$name"
    done
  fi
  printf '\nchecks after install:\n'
  printf '  %s --help\n' "$DEST"
  if (( SKIP_GPU )); then :; else
    printf '  glxinfo -B / vulkaninfo --summary\n'
    printf '  systemctl --user is-system-running / loginctl show-user %s -p Linger\n' "$TARGET_USER"
  fi
  exit 0
fi

mkdir -p "$(dirname "$DEST")" "${HOME}/.local/bin"

if [ -n "$installed_sha" ] && [ -n "$SHA256" ] && [ "$installed_sha" = "$SHA256" ]; then
  printf 'already installed: %s\n' "$DEST"
else
  tmp="${DEST}.new"
  trap 'rm -f "$tmp"' EXIT
  if [ -n "$local_src" ]; then
    cp -p "$local_src" "$tmp"
  else
    curl -fL --retry 3 --retry-delay 2 -o "$tmp" "$URL"
    printf '%s  %s\n' "$SHA256" "$tmp" | sha256sum -c -
  fi
  chmod 0755 "$tmp"
  if [ -f "$DEST" ]; then
    cp -p "$DEST" "${DEST}.previous"
  fi
  mv -f "$tmp" "$DEST"
  trap - EXIT
  printf 'installed: %s\n' "$DEST"
fi
ln -sfn "$DEST" "${HOME}/.local/bin/romm-shelf"

if ! printf '%s' ":$PATH:" | grep -q ":${HOME}/.local/bin:"; then
  if ! grep -q '\.local/bin' "${HOME}/.profile" 2>/dev/null; then
    printf '\n# romm-shelf: ~/.local/bin\ncase ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac\n' \
      >>"${HOME}/.profile"
    printf 'added ~/.local/bin to PATH in ~/.profile\n'
  fi
fi

if ! "$DEST" --help >/dev/null 2>&1; then
  if APPIMAGE_EXTRACT_AND_RUN=1 "$DEST" --help >/dev/null 2>&1; then
    warn "the AppImage could not FUSE-mount; it runs with APPIMAGE_EXTRACT_AND_RUN=1"
  else
    die "the AppImage does not run: $DEST --help failed"
  fi
fi

if [ -n "$PACKS" ]; then
  IFS=',' read -r -a pack_names <<<"$PACKS"
  for name in "${pack_names[@]}"; do
    [ -n "$name" ] || continue
    "$DEST" packs install "$name"
  done
else
  printf '# optional: %s packs install retroarch\n' "$DEST"
fi

if (( SKIP_GPU )); then :; else
  printf '\n'
  gpu_report
fi

cat <<EOF

Next:
  romm-shelf login https://your-romm-server
  romm-shelf packs install retroarch     (and dolphin, pcsx2, ... as wanted)
  romm-shelf serve --ui                  open http://127.0.0.1:8766 in Windows
  romm-shelf play <rom>                  no Steam here, so no Steam Input
EOF
