#!/usr/bin/env bash
# The OSWorld machine's startup script. Compute Engine runs it as root on every boot, so each step
# checks before it acts and a restart only repeats what is missing.
#
# In order: the idle shutdown (first, so even a failed boot powers off), the osworld user and the
# osworld-exec launcher, a KVM check, Docker from Docker's apt repository, uv, this repo in
# /opt/typesafe-computer-use, and `scripts/osworld setup`. It logs to /var/log/osworld-startup.log and ends by creating
# /run/osworld-ready, which scripts/osworld-gcp waits for; a failure creates
# /run/osworld-startup.failed instead. Both live in /run, so every boot starts without them.
set -euo pipefail

LOG=/var/log/osworld-startup.log
READY=/run/osworld-ready
FAILED=/run/osworld-startup.failed
USER_NAME=osworld                  # scripts/osworld-gcp: REMOTE_USER
REPO_DIR=/opt/typesafe-computer-use # scripts/osworld-gcp: REMOTE_DIR
UV_VERSION=0.12.19

exec > >(tee -a "$LOG") 2>&1
rm -f "$READY" "$FAILED"

log() { echo "[osworld-startup $(date -u +%H:%M:%S)] $*"; }
fail() {
  log "$*"
  exit 1
}
on_exit() {
  local status=$?
  if ((status != 0)); then
    log "FAILED with status $status; the lines above say where"
    touch "$FAILED"
  fi
}
trap on_exit EXIT

metadata() {
  curl -fsS --retry 10 --retry-connrefused -H 'Metadata-Flavor: Google' \
    "http://metadata.google.internal/computeMetadata/v1/instance/attributes/$1"
}

log "boot $(cat /proc/sys/kernel/random/boot_id)"
IDLE_MINUTES=$(metadata idle-shutdown-minutes)
REPO_URL=$(metadata repo-url)
REPO_REF=$(metadata repo-ref)

# --- Guard 1: idle shutdown -----------------------------------------------------------------------
log "idle shutdown after $IDLE_MINUTES minutes (0 means never)"
install -d -m 0755 /etc/osworld
printf '%s\n' "$IDLE_MINUTES" >/etc/osworld/idle-shutdown-minutes

cat >/usr/local/sbin/osworld-idle-check <<'EOF'
#!/usr/bin/env bash
# Powers the machine off once it has been idle for idle-shutdown-minutes (instance metadata, read
# on every check so a change applies without a reboot; 0 turns this off). Busy means a run_multienv
# process or an SSH connection. osworld-idle.timer runs this every 5 minutes. The last busy time
# lives in /run, so each boot starts a fresh clock.
set -euo pipefail
STATE=/run/osworld-idle/last-busy

minutes=$(curl -fsS -m 5 -H 'Metadata-Flavor: Google' \
  http://metadata.google.internal/computeMetadata/v1/instance/attributes/idle-shutdown-minutes \
  2>/dev/null || cat /etc/osworld/idle-shutdown-minutes)
if [[ ! $minutes =~ ^[0-9]+$ ]]; then
  echo "idle-shutdown-minutes is not a whole number: $minutes"
  exit 1
fi
((minutes > 0)) || exit 0

now=$(date +%s)
busy=
if pgrep -f run_multienv >/dev/null; then
  busy="an OSWorld run"
elif [[ -n $(who) ]]; then
  busy="a login session"
elif [[ -n $(ss -Htn state established '( sport = :22 )') ]]; then
  busy="an SSH connection"
fi

mkdir -p "${STATE%/*}"
if [[ -n $busy || ! -s $STATE ]]; then
  echo "$now" >"$STATE"
fi
idle=$(((now - $(cat "$STATE")) / 60))
if [[ -n $busy ]]; then
  echo "busy: $busy"
elif ((idle >= minutes)); then
  echo "idle for $idle minutes, limit $minutes: powering off"
  systemctl poweroff
else
  echo "idle for $idle of $minutes minutes"
fi
EOF
chmod 0755 /usr/local/sbin/osworld-idle-check

cat >/etc/systemd/system/osworld-idle.service <<'EOF'
[Unit]
Description=Power the OSWorld machine off when it has been idle too long

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/osworld-idle-check
EOF

cat >/etc/systemd/system/osworld-idle.timer <<'EOF'
[Unit]
Description=Check every 5 minutes whether the OSWorld machine is idle

[Timer]
OnBootSec=5min
OnUnitActiveSec=5min
AccuracySec=30s

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now osworld-idle.timer

# --- The unprivileged user that owns the repo and runs OSWorld ------------------------------------
if ! id -u "$USER_NAME" >/dev/null 2>&1; then
  log "creating user $USER_NAME"
  useradd --create-home --shell /bin/bash "$USER_NAME"
fi
USER_HOME=$(getent passwd "$USER_NAME" | cut -d: -f6)
USER_PATH="$USER_HOME/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

as_user() { sudo -u "$USER_NAME" -H env PATH="$USER_PATH" "$@"; }

# How scripts/osworld-gcp runs a command on the machine: `sudo -u osworld -H osworld-exec CMD...`.
cat >/usr/local/bin/osworld-exec <<EOF
#!/usr/bin/env bash
# Runs a command in $REPO_DIR with uv on PATH, OSWORLD_PROVIDER defaulting to docker, and the
# keys from .env, read the way jev reads them (typesafe_computer_use.config.load_dotenv: no shell
# evaluation, and the environment wins). startup.sh writes this file on every boot.
set -euo pipefail
cd $REPO_DIR
export PATH="\$HOME/.local/bin:\$PATH"
export OSWORLD_PROVIDER="\${OSWORLD_PROVIDER:-docker}"
exec python3 -c 'import os, sys; sys.path.insert(0, os.getcwd()); from pathlib import Path; from typesafe_computer_use.config import load_dotenv; load_dotenv(Path(".env")); os.execvp(sys.argv[1], sys.argv[1:])' "\$@"
EOF
chmod 0755 /usr/local/bin/osworld-exec

# --- KVM ------------------------------------------------------------------------------------------
if [[ ! -e /dev/kvm ]]; then
  fail "/dev/kvm is missing, so nested virtualization is off and OSWorld's VM cannot run. Check that
the machine type is an Intel one (n2 is), that the instance has enable_nested_virtualization, and
that the organization policy constraints/compute.disableNestedVirtualization is not enforced."
fi
log "/dev/kvm is present"

# --- Packages and Docker CE -----------------------------------------------------------------------
export DEBIAN_FRONTEND=noninteractive
apt_get() { apt-get -q -y -o DPkg::Lock::Timeout=600 "$@"; }

missing=()
for tool in git rsync curl python3; do
  command -v "$tool" >/dev/null || missing+=("$tool")
done
for lib in libGL.so.1 libglib-2.0.so.0; do
  ldconfig -p | grep -q "$lib" || missing+=("$lib")
done
command -v gcc >/dev/null || missing+=(gcc)
[[ -e /usr/include/linux/input.h ]] || missing+=(linux/input.h)
python3 -c 'import sysconfig, os, sys; sys.exit(not os.path.exists(os.path.join(sysconfig.get_path("include"), "Python.h")))' ||
  missing+=(Python.h)
if ((${#missing[@]})); then
  log "installing packages for ${missing[*]}"
  apt_get update
  # libgl1 and libglib2.0-0: rapidocr's opencv-python (not headless) loads libGL and GLib at import.
  # build-essential, linux-libc-dev, and python3-dev: OSWorld's lock builds some packages from
  # source, among them evdev (pynput's), a C extension that compiles against the kernel's input
  # headers and against Python.h of the system Python 3.12 that uv picks for OSWorld's venv.
  apt_get install git rsync curl ca-certificates python3 python3-dev libgl1 libglib2.0-0 \
    build-essential linux-libc-dev
fi

if ! dpkg-query -W -f='${Status}' docker-ce 2>/dev/null | grep -q 'install ok installed'; then
  log "installing Docker CE from Docker's apt repository"
  apt_get update
  apt_get install ca-certificates curl
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  # shellcheck disable=SC1091 # the machine's own file
  codename=$(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc]" \
    "https://download.docker.com/linux/ubuntu $codename stable" >/etc/apt/sources.list.d/docker.list
  apt_get update
  apt_get install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker
getent group kvm >/dev/null || groupadd --system kvm
usermod -aG docker,kvm "$USER_NAME"
log "$(docker --version)"

# --- uv, for the osworld user ---------------------------------------------------------------------
uv_version=$(as_user uv --version 2>/dev/null || true)
if [[ $uv_version != "uv $UV_VERSION"* ]]; then
  log "installing uv $UV_VERSION"
  as_user sh -c "curl -LsSf https://astral.sh/uv/$UV_VERSION/install.sh | sh"
fi
log "$(as_user uv --version)"

# --- This repo --------------------------------------------------------------------------------------
# scripts/osworld-gcp syncs a local working copy over the checkout (everything but .git) before
# each run. So the checkout only moves to repo-ref while its tracked files still match git; once a
# local copy is synced, the next sync replaces it anyway.
install -d -o "$USER_NAME" -g "$USER_NAME" -m 0755 "$REPO_DIR"
repo_git() { as_user git -C "$REPO_DIR" "$@"; }

[[ -d $REPO_DIR/.git ]] || repo_git -c init.defaultBranch=main init --quiet
if repo_git remote get-url origin >/dev/null 2>&1; then
  repo_git remote set-url origin "$REPO_URL"
else
  repo_git remote add origin "$REPO_URL"
fi
repo_git fetch --quiet --force --tags origin
if repo_git rev-parse --verify --quiet "refs/remotes/origin/$REPO_REF^{commit}" >/dev/null; then
  target="origin/$REPO_REF"
elif repo_git rev-parse --verify --quiet "$REPO_REF^{commit}" >/dev/null; then
  target=$REPO_REF
else
  repo_git fetch --quiet origin "$REPO_REF"
  target=FETCH_HEAD
fi

if ! repo_git rev-parse --verify --quiet HEAD >/dev/null; then
  log "checking out $REPO_REF from $REPO_URL"
  repo_git checkout --quiet --force --detach "$target"
elif [[ -z $(repo_git status --porcelain --untracked-files=no) ]]; then
  log "moving the checkout to $REPO_REF"
  repo_git checkout --quiet --detach "$target"
else
  log "the checkout holds a synced local copy; leaving it as it is"
fi
log "repo at $(repo_git rev-parse --short HEAD)"

# --- OSWorld ----------------------------------------------------------------------------------------
[[ -x $REPO_DIR/scripts/osworld ]] || fail "$REPO_DIR/scripts/osworld is missing: repo-ref $REPO_REF is older than it"
log "scripts/osworld setup"
# shellcheck disable=SC2016 # $1 expands in the inner shell
as_user env OSWORLD_PROVIDER=docker OSWORLD_OCR=rapidocr \
  sh -c 'cd "$1" && exec scripts/osworld setup' sh "$REPO_DIR"

touch "$READY"
log "ready"
