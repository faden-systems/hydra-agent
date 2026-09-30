#!/usr/bin/env bash
# Root-only Ubuntu 24.04 infrastructure bootstrap. No authentication or factory loops; installs the manager
# (loops/b1.md) from the hydra-agent clone without starting it.
# Exit 0: provisioned; 2: infrastructure ready, private clone PENDING; 1: other failure.
# Sourceable for portable unit tests; main is never dispatched when sourced.

ROOT=/srv/hydra
MANAGER=$ROOT/manager
REPOS=$ROOT/repos
TOOLS=/opt/hydra-tools
NODE=/opt/hydra-node
STATE=/var/lib/hydra-bootstrap
UNIT=/etc/systemd/system/hydra-manager.service
UNIT_DIR=/etc/systemd/system

fail() { printf 'ERROR: %s\n' "$*" >&2; return 1; }

check_platform() {
    [[ $(uname -s) == Linux ]] || { fail 'Requires Ubuntu 24.04 Linux'; return 1; }
    [[ -r /etc/os-release ]] || { fail 'Missing /etc/os-release'; return 1; }
    # shellcheck disable=SC1091
    . /etc/os-release
    [[ ${ID:-} == ubuntu && ${VERSION_ID:-} == 24.04 ]] || {
        fail 'Requires Ubuntu 24.04'; return 1;
    }
    case $(uname -m) in x86_64|aarch64) ;; *) fail 'Requires amd64 or arm64'; return 1;; esac
}

check_root() { [[ $(id -u) == 0 ]] || fail 'Run as root'; }

safe_directory() {
    [[ ! -L $1 ]] || { fail "Refusing symlink: $1"; return 1; }
    mkdir -p "$1"
}

apt_missing() {
    local package status
    local missing=()
    for package in "$@"; do
        status=$(dpkg-query -W -f='${Status}' "$package" 2>/dev/null) || status=''
        [[ $status == 'install ok installed' ]] || missing+=("$package")
    done
    if ((${#missing[@]})); then
        apt-get install -y --no-upgrade "${missing[@]}"
    fi
}

install_packages() {
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt_missing ca-certificates curl xz-utils software-properties-common
    # fonts-inter and several browser libraries are in Noble universe.
    if [[ ! -f $STATE/universe.ready ]]; then
        add-apt-repository -y universe
        touch "$STATE/universe.ready"
    fi
    if [[ ! -f /etc/apt/sources.list.d/hydra-tailscale.list ]]; then
        curl --fail --silent --show-error --location \
            https://pkgs.tailscale.com/stable/ubuntu/noble.noarmor.gpg \
            -o "$STATE/tailscale.gpg"
        install -o root -g root -m 0644 "$STATE/tailscale.gpg" /usr/share/keyrings/hydra-tailscale.gpg
        printf '%s\n' 'deb [signed-by=/usr/share/keyrings/hydra-tailscale.gpg] https://pkgs.tailscale.com/stable/ubuntu noble main' \
            > /etc/apt/sources.list.d/hydra-tailscale.list
    fi
    apt-get update
    # gh and rclone come from the official Ubuntu archive; Tailscale from its signed stable repo.
    apt_missing git gh python3.12 python3.12-venv rclone tailscale tmux \
        fonts-inter fonts-noto-core fonts-noto-color-emoji
    systemctl enable --now tailscaled.service
    # No tailscale enrollment here. Packages already present are not upgraded or held.
}

prepare_user() {
    if ! id hydra >/dev/null 2>&1; then
        useradd --create-home --home-dir /home/hydra --shell /bin/bash --user-group hydra
    fi
    [[ $(id -u hydra) != 0 ]] || { fail 'hydra must not be UID 0'; return 1; }
    [[ $(getent passwd hydra | cut -d: -f6) == /home/hydra ]] || {
        fail 'Existing hydra account has unexpected home'; return 1;
    }
    [[ $(id -gn hydra) == hydra ]] || { fail 'hydra must have its own primary group'; return 1; }
    # No supplementary groups (especially sudo, docker, lxd); no password login.
    usermod -G '' -s /bin/bash hydra
    usermod --lock hydra
    if command -v visudo >/dev/null 2>&1; then
        printf '%s\n' 'hydra ALL=(ALL:ALL) !ALL' > "$STATE/hydra-no-sudo"
        visudo -cf "$STATE/hydra-no-sudo"
        install -o root -g root -m 0440 "$STATE/hydra-no-sudo" /etc/sudoers.d/zz-hydra-no-sudo
        visudo -c
    fi
}

prepare_layout() {
    local directory
    # Root-owned parents protect the bootstrap and the installed code (app/), not writable state contents.
    for directory in "$ROOT" "$MANAGER"; do
        safe_directory "$directory"
        chown root:root "$directory"
        chmod 0755 "$directory"
    done
    # The manager home holds the supervisor's own state (WRITER, PAUSE, engine, session-id, state.json,
    # MANAGER-HANDOFF.md): hydra-owned, group root, not world-readable.
    chown hydra:root "$MANAGER"
    chmod 0750 "$MANAGER"
    for directory in "$MANAGER/.claude" "$MANAGER/credentials" "$MANAGER/inbox" "$MANAGER/inbox/files" \
        "$MANAGER/inbox/replies" "$MANAGER/logs" "$MANAGER/mirror" "$REPOS"; do
        safe_directory "$directory"
        chown hydra:hydra "$directory"
        chmod 0700 "$directory"
    done
    # Never recursively chown, delete, populate or reset existing state or repositories.
}

install_node() {
    local version arch archive work
    case $(uname -m) in x86_64) arch=x64;; aarch64) arch=arm64;; esac
    if [[ ! -f $STATE/node.version ]]; then
        curl --fail --silent --show-error --location https://nodejs.org/dist/index.json -o "$STATE/node-index.json"
        version=$(python3.12 -c 'import json,sys; print(next(r["version"] for r in json.load(open(sys.argv[1])) if r["lts"]))' "$STATE/node-index.json")
        [[ $version =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { fail 'Invalid Node LTS version'; return 1; }
        printf '%s\n' "$version" > "$STATE/node.version"
    fi
    version=$(< "$STATE/node.version")
    [[ $version =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { fail 'Invalid saved Node version'; return 1; }
    if [[ ! -f $STATE/node.installed ]]; then
        work=$(mktemp -d "$STATE/node.XXXXXX")
        archive="node-$version-linux-$arch.tar.xz"
        curl --fail --silent --show-error --location "https://nodejs.org/dist/$version/$archive" -o "$work/$archive"
        curl --fail --silent --show-error --location "https://nodejs.org/dist/$version/SHASUMS256.txt" -o "$work/SHASUMS256.txt"
        (cd "$work"; python3.12 -c 'import sys; p=sys.argv[1]; rows=[s for s in open("SHASUMS256.txt") if s.split()[-1]==p]; assert len(rows)==1; open("CHECKSUM","w").writelines(rows)' "$archive"
            sha256sum --check CHECKSUM)
        safe_directory "$NODE"
        tar -xJf "$work/$archive" --strip-components=1 -C "$NODE" --no-same-owner
        touch "$STATE/node.installed"
        rm -rf -- "$work"
    fi
    [[ $("$NODE/bin/node" --version) == "$version" ]] || { fail 'Node installation/version mismatch'; return 1; }
    ln -sfn "$NODE/bin/node" /usr/local/bin/node
    ln -sfn "$NODE/bin/npm" /usr/local/bin/npm
    ln -sfn "$NODE/bin/npx" /usr/local/bin/npx
}

install_npm_tool() {
    local package=$1 name=$2 version
    if [[ ! -f $STATE/$name.version ]]; then
        version=$(npm view "$package" dist-tags.latest)
        [[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { fail "Invalid $name version"; return 1; }
        printf '%s\n' "$version" > "$STATE/$name.version"
    fi
    version=$(< "$STATE/$name.version")
    [[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { fail "Invalid saved $name version"; return 1; }
    if [[ ! -f $STATE/$name.installed ]]; then
        npm install --global --prefix "$TOOLS" --no-audit --no-fund "$package@$version"
        touch "$STATE/$name.installed"
    fi
}

install_tools() {
    safe_directory "$TOOLS"
    export PATH="$TOOLS/bin:$NODE/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    # Official npm registry; do not inherit an operator's npm auth/config.
    # npm rejects loading the same path for both user and global config.
    : > "$STATE/npm-globalrc"
    export npm_config_userconfig=/dev/null npm_config_globalconfig="$STATE/npm-globalrc"
    export npm_config_registry=https://registry.npmjs.org/
    export npm_config_cache="$STATE/npm-cache"
    install_node
    install_npm_tool @anthropic-ai/claude-code claude
    install_npm_tool @openai/codex codex
    install_npm_tool playwright playwright
    local tool
    for tool in claude codex playwright; do
        [[ -x $TOOLS/bin/$tool ]] || { fail "Missing installed tool: $tool"; return 1; }
        ln -sfn "$TOOLS/bin/$tool" "/usr/local/bin/$tool"
    done
    # Only system dependencies, not browsers or an agent framework. Repos install their own browsers.
    if [[ ! -f $STATE/playwright-deps.installed ]]; then
        "$TOOLS/bin/playwright" install-deps
        touch "$STATE/playwright-deps.installed"
    fi
    dpkg-query -W -f='${binary:Package}\t${Version}\n' > "$STATE/apt-versions.tsv"
}

as_hydra() {
    # Root's GH_TOKEN/GITHUB_TOKEN, git config, HOME and other secrets never cross this boundary.
    runuser -u hydra -- env -i HOME=/home/hydra USER=hydra LOGNAME=hydra \
        PATH="$TOOLS/bin:$NODE/bin:/usr/local/bin:/usr/bin:/bin" \
        CLAUDE_CONFIG_DIR="$MANAGER/.claude" GH_CONFIG_DIR=/home/hydra/.config/gh \
        GIT_TERMINAL_PROMPT=0 GH_PROMPT_DISABLED=1 "$@"
}

clone_repositories() {
    local name destination pending=0
    for name in faden hydra-agent; do
        destination=$REPOS/$name
        if [[ -e $destination || -L $destination ]]; then
            [[ ! -L $destination && -d $destination/.git ]] || {
                fail "Existing path is not a regular clone; preserve and inspect: $destination"; return 1;
            }
            printf 'PRESERVED: %s (no fetch, reset, checkout or pull)\n' "$destination"
            continue
        fi
        if [[ $name == faden ]] && ! as_hydra /usr/bin/gh auth status --hostname github.com >/dev/null 2>&1; then
            printf 'PENDING: private faden clone requires human GitHub login as hydra.\n'
            pending=2
            continue
        fi
        # Empty helper first overrides inherited per-user helpers; gh reads hydra's human-provided login.
        if ! as_hydra /usr/bin/git -c credential.helper= \
            -c 'credential.helper=!/usr/bin/gh auth git-credential' \
            clone "https://github.com/faden-systems/$name.git" "$destination"; then
            if [[ $name == faden ]]; then
                printf 'PENDING: private faden clone failed; check hydra GitHub access/network and rerun.\n'
                pending=2
            else
                fail 'Public hydra-agent clone failed'; return 1
            fi
        fi
    done
    return "$pending"
}

install_service() {
    [[ ! -L $MANAGER/supervisor.sh ]] || { fail 'Refusing supervisor symlink'; return 1; }
    if [[ ! -e $MANAGER/supervisor.sh ]]; then
        cat > "$MANAGER/supervisor.sh" <<'SUPERVISOR'
#!/usr/bin/env bash
set -eu
# Infrastructure placeholder only: no model calls, work dispatch, or factory loop.
while true; do
    printf 'hydra-manager heartbeat %s\n' "$(date -u +%FT%TZ)"
    sleep 300
done
SUPERVISOR
    fi
    chown root:root "$MANAGER/supervisor.sh"
    chmod 0755 "$MANAGER/supervisor.sh"
    cat > "$UNIT" <<EOF
[Unit]
Description=Hydra manager infrastructure heartbeat (no AI calls)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=hydra
Group=hydra
WorkingDirectory=$MANAGER
Environment=HOME=/home/hydra
Environment=CLAUDE_CONFIG_DIR=$MANAGER/.claude
Environment=PATH=$TOOLS/bin:$NODE/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=$MANAGER/supervisor.sh
Restart=on-failure
RestartSec=10
UMask=0077
NoNewPrivileges=true
CapabilityBoundingSet=
RestrictSUIDSGID=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=$MANAGER/.claude $MANAGER/credentials $MANAGER/inbox $MANAGER/logs $REPOS /home/hydra
StandardOutput=journal
StandardError=journal
SyslogIdentifier=hydra-manager

[Install]
WantedBy=multi-user.target
EOF
    chown root:root "$UNIT"
    chmod 0644 "$UNIT"
    systemctl daemon-reload
    # Do not restart a future supervisor on reruns.
    systemctl enable --now hydra-manager.service
    systemctl is-active --quiet hydra-manager.service
}

configure_manager_repo() {
    # Config is the service's source of truth: never override an operator's repo.
    [[ ! -L $MANAGER ]] || { fail 'Refusing manager symlink'; return 1; }
    # Write as hydra, not root, so both the temporary file and replacement are safely owned.
    as_hydra python3.12 - "$MANAGER/config.json" "$REPOS/faden" <<'PY'
import json
import os
from pathlib import Path
import sys
import tempfile

config = Path(sys.argv[1])
if config.is_symlink():
    sys.exit("Refusing config.json symlink")
try:
    data = json.loads(config.read_text())
except FileNotFoundError:
    data = {}
if not isinstance(data, dict):
    sys.exit("config.json must contain a JSON object")
if "repo" in data:
    sys.exit(0)
data["repo"] = sys.argv[2]
# Same-directory replacement: readers see either the old complete JSON or the new one.
fd, temporary = tempfile.mkstemp(prefix=".config.json.", dir=config.parent)
try:
    with os.fdopen(fd, "w") as output:
        json.dump(data, output, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, config)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY
}

install_manager() {
    # The manager from the hydra-agent checkout (loops/b1.md): code to $MANAGER/app/manager/, rules to
    # $MANAGER/CLAUDE.md, the project venv, both units, the hydra CLI. Replaces the heartbeat unit written by
    # install_service; the running process is not restarted here (a human starts the services, see manager-vm.md).
    local source=${HYDRA_MANAGER_SRC:-$REPOS/hydra-agent/manager} unit
    if [[ ! -f $source/supervisor.py || ! -f $source/bridge.py || ! -f $source/hydra ]]; then
        printf 'PENDING: manager source not found at %s; pull the hydra-agent clone and rerun.\n' "$source"
        return 0
    fi
    configure_manager_repo || return 1
    [[ ! -L $MANAGER/app ]] || { fail 'Refusing app symlink'; return 1; }
    safe_directory "$MANAGER/app"
    rm -rf -- "$MANAGER/app/manager.new"
    cp -R -- "$source" "$MANAGER/app/manager.new"
    rm -rf -- "$MANAGER/app/manager"
    mv -- "$MANAGER/app/manager.new" "$MANAGER/app/manager"
    chown -R root:root "$MANAGER/app"
    chmod -R u=rwX,go=rX "$MANAGER/app"
    chmod 0755 "$MANAGER/app/manager/hydra" "$MANAGER/app/manager/supervisor.py" "$MANAGER/app/manager/bridge.py"
    # The manager's standing rules, root-owned so the session cannot rewrite them.
    install -o root -g root -m 0644 "$MANAGER/app/manager/CLAUDE.md" "$MANAGER/CLAUDE.md"
    # Codex reads AGENTS.md in the engine working directory; share the installed standing rules.
    ln -sfnT app/manager/CLAUDE.md "$MANAGER/AGENTS.md"
    ln -sfn "$MANAGER/app/manager/hydra" /usr/local/bin/hydra
    # Both systemd units invoke this runtime venv directly, not the checkout's test .venv.
    [[ ! -L $MANAGER/venv ]] || { fail 'Refusing venv symlink'; return 1; }
    if ! "$MANAGER/venv/bin/python" -c 'import slack_bolt, slack_sdk' >/dev/null 2>&1; then
        python3.12 -m venv "$MANAGER/venv"
        PIP_CACHE_DIR="$STATE/pip-cache" "$MANAGER/venv/bin/python" -m pip install --quiet slack_bolt slack_sdk
    fi
    chown -R hydra:hydra "$MANAGER/venv"
    for unit in hydra-manager.service hydra-bridge.service; do
        install -o root -g root -m 0644 "$MANAGER/app/manager/systemd/$unit" "$UNIT_DIR/$unit"
    done
    systemctl daemon-reload
    systemctl enable hydra-manager.service hydra-bridge.service
    # Not started or restarted here: credentials/slack.env, allowlist.json and config.json are human steps first.
    printf 'MANAGER: installed to %s/app/manager; units enabled, not (re)started.\n' "$MANAGER"
}

main() {
    set -euo pipefail
    check_platform
    check_root
    umask 022
    export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
    safe_directory "$STATE"
    chown root:root "$STATE"
    chmod 0700 "$STATE"
    # Serialize provisioning, including resolve-once version selection.
    exec 9>"$STATE/lock"
    flock -n 9 || { fail 'Another bootstrap is running'; return 1; }
    install_packages
    prepare_user
    prepare_layout
    install_tools
    if [[ $(readlink -f "${BASH_SOURCE[0]}") != /usr/local/sbin/hydra-manager-bootstrap ]]; then
        install -o root -g root -m 0755 "${BASH_SOURCE[0]}" /usr/local/sbin/hydra-manager-bootstrap
    fi
    install_service
    local result=0
    clone_repositories || result=$?
    install_manager
    if [[ $result == 2 ]]; then
        printf '%s\n' 'PENDING: infrastructure and heartbeat ready. A human must run gh auth login as hydra, then rerun this bootstrap (exit 2).'
    elif [[ $result == 0 ]]; then
        printf '%s\n' 'READY: infrastructure and clones present; heartbeat only. No AI, GitHub, Tailscale or backup authentication was performed.'
    fi
    return "$result"
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    main "$@"
fi
