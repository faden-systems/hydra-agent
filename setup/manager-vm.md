# Headless manager VM: human-gated provisioning

Scope: Ubuntu 24.04, one `hydra` service user, Claude Code and Codex CLI, and the
manager (`manager/`, spec `loops/b1.md`) installed but **not started**: section 8
below is the human-gated start. No coder service, backups, Hermes, OpenClaw, or
other agent framework is installed or launched. See
[`docs/architecture.md`](../docs/architecture.md) sections 2 and 4 and
[`docs/setup.md`](../docs/setup.md). This is infrastructure preparation, not the
complete `m1` deployment. In particular the architecture's separate coder account
and its service user are out of scope.

## 1. Agent-led GCP provisioning from the iMac (human-gated)

L authorizes the project and spend; the agent on the **iMac** can create the VM
with the reviewed script. L need not manually provision a VPS. Code preparation,
cloud creation, and remote bootstrap are separate gates: do not execute creation
or bootstrap just to test this PR. Review/merge the `setup/manager-vm` PR or
explicitly approve its exact revision before deployment.

Prerequisites, checked on the iMac after L's human login:

1. Install the Google Cloud CLI and let L complete `gcloud auth login` privately.
   Check `gcloud auth list` and `gcloud projects list`; a browser-console login
   alone does not authenticate the CLI. L supplies the intended **project ID**.
2. L completes any terms acceptance, project creation, billing linkage and Compute
   Engine API enablement. The CLI principal needs permission to create the VM and
   use the project's network/service account. Report failures and stop; do not
   silently switch projects or enable paid services beyond approval.
3. Ensure `~/.ssh/id_ed25519.pub` contains the iMac's Ed25519 **public** key. Preserve
   existing keys; never copy the private key to GCP, the VM, the repo or chat.
   The script rejects missing/malformed key text and metadata delimiters.
4. Confirm the project permits metadata-based SSH keys (OS Login, if enforced,
   requires a separately approved IAM/OS Login path). The requested command uses
   the default VPC; it must exist and allow the approved SSH route from the iMac.
   The `hydra-manager` tag does **not** create a firewall rule. Do not open SSH to
   the world or change organization policy to work around a failure.

After explicit creation authorization, from the reviewed checkout on the iMac:

```sh
PROJECT_ID='REPLACE_WITH_APPROVED_PROJECT_ID'
bash setup/gcp-vm.sh "$PROJECT_ID"
```

The project argument is mandatory; the script never relies on a CLI/environment
project default. It issues one `gcloud compute instances create hydra-manager`
with zone `us-west1-b`, machine `e2-standard-4` (4 vCPU, 16 GB RAM), image family
`ubuntu-2404-lts-amd64` in `ubuntu-os-cloud`, a **100GB pd-balanced** boot disk,
`ssh-keys=hermes:<iMac public key>` metadata, and tag `hydra-manager`. This is Ubuntu
24.04 LTS amd64 with systemd; working DNS/outbound HTTPS is required for bootstrap.
The image family tracks Google's current family image, not an immutable image pin.
No startup/bootstrap script, SSH session, firewall mutation or factory loop runs.
Creation is **not idempotent**: an existing name returns a gcloud error; the script
propagates failure without retrying, deleting or replacing an existing VM. If the
VM already exists, inspect it rather than rerunning creation.

After successful creation (or for the already-created VM), inspect it explicitly:

```sh
gcloud compute instances describe hydra-manager \
  --project "$PROJECT_ID" --zone us-west1-b
```

Record the actual IP, status, zone, sizing and SSH identity. Verify the SSH host-key
fingerprint through the trusted provider console before connecting; never disable
host-key checking. Do not report creation success after a failed create operation.

## 2. Bootstrap over SSH as hermes (after target verification and approval)

The metadata key maps to the **hermes admin account**, with sudo provided by the
GCP guest environment when metadata SSH is supported. Verify that access on the
actual VM; **no root SSH is needed**. `hydra` is the separate, unprivileged service
user, not the SSH admin: "sudo-less shell" means `/bin/bash` with **no sudo
entitlement**, not passwordless sudo. The bootstrap locks hydra's password and
removes its supplementary groups. Use a dedicated VM, not a shared hydra account.

From the reviewed checkout on the iMac (replace `VM_IP` with the verified address):

```sh
ssh -i ~/.ssh/id_ed25519 hermes@VM_IP 'id; sudo -n true'
scp -i ~/.ssh/id_ed25519 setup/manager-vm.sh hermes@VM_IP:/tmp/hydra-manager-vm.sh
ssh -t -i ~/.ssh/id_ed25519 hermes@VM_IP 'sudo bash /tmp/hydra-manager-vm.sh'
```

If sudo is unavailable, stop and resolve admin access with L; do not enable root
SSH. If a sudo password is required, L supplies it directly, never in chat. The
existing bootstrap runs under sudo without modification and does not copy any
local login, token or environment.

Exit codes:
- **0**: packages, directories, heartbeat service and both clones are present.
- **2 / PENDING**: infrastructure is ready, but the private `faden` clone needs the
  human GitHub login/access check below. This is **not** full provisioning success.
- **1** (or another nonzero code): failure; inspect the actual error before retrying.

`faden-systems/faden` is private, so a clean machine normally returns **2**.
`faden-systems/hydra-agent` is public. Do not put a GitHub token in a clone URL,
script argument or repository just to make the first run return zero.

### What is installed

- Ubuntu packages: git, gh, Python 3.12 + venv, rclone, tmux, Inter, Noto Sans
  (`fonts-noto-core`) and Noto Color Emoji; universe is enabled.
- Tailscale from its signed official Noble repository; `tailscaled` runs but is
  **not enrolled** until L authorizes it.
- Official Node LTS from nodejs.org, SHA-256 checked; official npm packages
  `@anthropic-ai/claude-code`, `@openai/codex`, `playwright`.
- Playwright **system dependencies**, not browsers. Each repo/harness installs its
  own matching browsers later. No repo dependencies or loop scripts are executed.

Node and npm tool versions are resolved once, recorded under
`/var/lib/hydra-bootstrap/*.version`, and retained on rerun. Node lives in
`/opt/hydra-node`, npm tools in `/opt/hydra-tools`, with links in `/usr/local/bin`.
Ubuntu packages already installed are not deliberately upgraded; the resulting
inventory is recorded as `apt-versions.tsv`. This is per-machine version retention,
not a cross-machine immutable image. Routine security updates remain an admin task.

Layout:

```text
/srv/hydra/manager/                 hydra:root 0750, the manager's home ($HYDRA_HOME); not a git checkout
  app/manager/                     root-owned copy of the repo's manager/ (supervisor.py, bridge.py, hydra, units)
  venv/                            root-owned project venv (slack_bolt); the entry points re-exec into it
  CLAUDE.md                        root-owned copy of manager/CLAUDE.md, the manager's standing rules
  supervisor.sh                    root-owned heartbeat placeholder, preserved on reruns, no longer run by the unit
  .claude/                         hydra:hydra 0700, initially empty
  credentials/                     hydra:hydra 0700, initially empty
  inbox/ inbox/files inbox/replies hydra:hydra 0700
  logs/ mirror/                    hydra:hydra 0700
  engine, session-id, state.json, MANAGER-HANDOFF.md, PAUSE, WRITER: written by the supervisor at runtime
/srv/hydra/repos/                   hydra:hydra 0700
  faden/                           private; deferred until authorized
  hydra-agent/                     public
/usr/local/sbin/hydra-manager-bootstrap
/usr/local/bin/hydra -> /srv/hydra/manager/app/manager/hydra
/etc/systemd/system/hydra-manager.service   the supervisor loop (replaces the heartbeat unit)
/etc/systemd/system/hydra-bridge.service    the Slack bridge
```

`install_manager` runs after the clones. It copies `manager/` from the hydra-agent
clone (override the source with `HYDRA_MANAGER_SRC=<path>`), installs both units and
**enables** them, and does not start or restart anything: the running heartbeat
keeps running until a human restarts the units in section 8. When the clone does
not contain `manager/` yet (it is never pulled automatically), the bootstrap prints
`PENDING: manager source not found` and continues; pull as hydra and rerun.

The systemd service sets `HOME=/home/hydra` and
`CLAUDE_CONFIG_DIR=/srv/hydra/manager/.claude`, runs as hydra with no new privileges,
and logs an immediate UTC heartbeat followed by one every **300 seconds** to the
journal. It does not load credentials or call an LLM. Logs are bounded by the
machine's journald policy. Existing supervisor contents, credential/config
contents, and dirty repository worktrees are preserved on rerun; repositories are
**never** pulled, reset or checked out automatically. A running supervisor is not
restarted on rerun. An intentional future supervisor/unit change needs separate
review and a controlled restart.

## 3. Claude R2D2 setup-token, with L in the thread

All auth is performed **as hydra**, never root. Do not run `claude auth login` or
plain interactive `claude` on this manager; runtime authentication is exclusively
`CLAUDE_CODE_OAUTH_TOKEN`. Keep the same manager config directory and working
folder across account rotation.

Open a clean hydra shell through the admin account:

```sh
sudo -u hydra env -i HOME=/home/hydra USER=hydra LOGNAME=hydra \
  PATH=/usr/local/bin:/usr/bin:/bin TERM=xterm-256color \
  CLAUDE_CONFIG_DIR=/srv/hydra/manager/.claude \
  /bin/bash --noprofile --norc
cd /srv/hydra/manager
umask 077
set +x
```

1. Run `claude setup-token` with that environment. This is an **interactive auth
   ceremony**, not a background service. Capture its output in a mode-0600 private
   temporary file or use a human-controlled terminal, because completion prints a
   bearer token. Do not let unfiltered output enter an agent transcript or Slack.
2. Post **only the authorization URL** to L in this thread. L signs in to the
   **R2D2** account and approves. Handle any callback/code privately in the terminal.
3. Store the resulting token only in
   `/srv/hydra/manager/credentials/claude-r2d2.env`, owner hydra, mode **0600**,
   containing `CLAUDE_CODE_OAUTH_TOKEN=...`. Do not put the literal token in shell
   history, command arguments, a repository, tool-visible output, or Slack.
   A human may enter it via the following hidden prompt **directly over SSH**:

   ```sh
   python3.12 -c 'import getpass, os, re; from pathlib import Path; t=getpass.getpass("R2D2 OAuth token (hidden): "); assert re.fullmatch(r"[A-Za-z0-9._~-]+",t), "Unexpected token format; inspect privately"; p=Path("credentials/claude-r2d2.env"); fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); f=os.fdopen(fd,"w"); f.write("CLAUDE_CODE_OAUTH_TOKEN="+t+"\n"); f.close()'
   ```

   The exclusive create intentionally refuses to overwrite an existing token.
   During agent-assisted setup, extract the token from the private capture locally
   into this same file **without printing it**, then delete the capture.
4. Check ownership/mode and absence of stored Claude login **without printing file
   contents**. `.claude` may contain nonsecret CLI metadata/transcripts after use,
   but must not contain `.credentials.json` or another persistent account login.
   If setup-token unexpectedly stores a login, stop and resolve it before testing.
5. Repeat later when L is available with the **L account**, using the same manager
   config directory and a separate `credentials/claude-l.env`, also 0600. The
   account-rotation test cannot pass until both accounts have been authorized.

Never source either token into a long-lived login profile or systemd unit. Only
load the selected token in the short-lived test/runtime subprocess.

## 4. Codex, GitHub, Tailscale (in that order)

1. In the same clean hydra shell, with neither Claude token sourced:
   `codex login --device-auth`. Post the **device URL and user code**, not any
   resulting credentials. L approves. Auth belongs to hydra's
   `/home/hydra/.codex/`, not root's home. Inspect permissions without displaying
   `auth.json`. Do not use an OpenAI API key for this subscription-login test.
2. As hydra, run `gh auth login --hostname github.com --git-protocol https
   --with-token` with L's fine-grained token provided through **private stdin**.
   Scope it to the intended Faden Systems repositories and required permissions
   (Contents read for cloning, Contents/Pull requests write for later PR work,
   plus org approval if required). GitHub CLI fine-grained-token limitations may
   require checking actual repo access rather than trusting auth status alone.
   Never echo the token, pass it as an argument, or commit it. The original handoff
   proposed DM delivery, but also prohibited tokens in Slack: prefer L entering it
   directly over SSH, or agree an approved non-Slack secret channel first. Do not
   request a token in this thread. Headless gh can store its auth file on disk;
   restrict `/home/hydra/.config/gh` to 0700 and its credential file to 0600.
   A human can run this **directly over SSH in the clean hydra shell**: input is
   hidden, and the token goes to gh's stdin, never its argv or shell history.

   ```sh
   python3.12 -c 'import getpass, subprocess; t=getpass.getpass("GitHub fine-grained token (hidden): "); subprocess.run(["gh","auth","login","--hostname","github.com","--git-protocol","https","--with-token"],input=t+"\n",text=True,check=True)'
   ```

   Run `gh auth setup-git --hostname github.com` as hydra for future git operations.
3. Exit the hydra shell and rerun as admin/root:

   ```sh
   sudo /usr/local/sbin/hydra-manager-bootstrap
   ```

   Require **exit 0** and both clones present. No need to repeat other login steps.
   A restricted token/organization approval/network failure remains PENDING.
4. As admin/root run `sudo tailscale up`. Post only the authorization URL. L
   enrolls the machine into the intended tailnet. Verify `tailscale status`. Do
   not change SSH/firewall/access policy or enable Tailscale SSH without L's
   approval. Keep the original admin SSH session until connectivity is verified.

No rclone authentication, remote or backup schedule is configured in this phase.
Future backups must exclude `credentials/`, Claude auth artifacts and both
`/home/hydra/.codex` and `/home/hydra/.config/gh` (or encrypt them separately).

### Explicit VM-only Codex decision — 2026-09-30 (L)

L approved running Codex **without its sandbox and without approval prompts** on
this non-production, single-purpose VM. This is not a production default. The
`hydra` user's actual home is `/home/hydra`; set these top-level defaults in
`/home/hydra/.codex/config.toml` (owner hydra:hydra, mode 0600):

```toml
sandbox_mode = "danger-full-access"
approval_policy = "never"
```

Codex commands now have the full permissions of the unprivileged `hydra` user;
read-only task wording is not a security boundary. Do **not** change the kernel
setting: `kernel.apparmor_restrict_unprivileged_userns` remains `1`. This decision
avoids the prior `bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted`
failure without weakening the host's kernel policy. Test B must use these defaults,
not override them with `--sandbox read-only`; keep its read-only prompt, clean
environment, both Claude tokens quarantined, and guaranteed restoration.

## 5. Test A — same Claude session across R2D2 → L

Requires **both** approved tokens. Use a clean hydra shell as above, with no
`ANTHROPIC_API_KEY`, alternate provider variables, stored Claude login, or inherited
token. Keep cwd `/srv/hydra/manager` and the same `CLAUDE_CONFIG_DIR`. Set `umask 077`.
Do not run another Claude session in this cwd during the test: `--continue` selects
its latest session. Capture exit codes and complete stdout/stderr to private logs;
never use shell tracing or log the environment.

```sh
SESSION_ID=$(python3.12 -c 'import uuid; print(uuid.uuid4())')
printf '%s\n' "$SESSION_ID" > logs/rotation-session-id.txt
(
  set -a
  . credentials/claude-r2d2.env
  set +a
  claude -p "remember the word pelican" --model claude-fable-5-1 \
    --session-id "$SESSION_ID" --output-format json
)
# Record actual exit code immediately. Stop on any failure, do not invent a pass.
(
  set -a; . credentials/claude-r2d2.env; set +a
  claude -p "Keep that word in memory. Reply acknowledged, without repeating it." \
    --model claude-fable-5-1 --continue --output-format json
)
(
  set -a; . credentials/claude-r2d2.env; set +a
  claude -p "We will switch accounts next. Keep the word for the next turn." \
    --model claude-fable-5-1 --continue --output-format json
)
(
  set -a; . credentials/claude-l.env; set +a
  claude -p "What word did I ask you to remember at the start?" \
    --model claude-fable-5-1 --resume "$SESSION_ID" --output-format json
)
```

Execute/capture these **one at a time**, not blindly as one pasted script. Retain
all four raw JSON results, stderr and exit codes; verify the session IDs match and
the last response says **pelican** without the fourth prompt supplying the word.
Post those raw results after a local secret check, redacting only actual secrets
if any appear and marking such redactions. Neither a handoff file nor a fresh
session answering from a supplied prompt demonstrates session rotation. If the
specified model is unavailable, report its raw error, not a substituted model's
success. An account/auth/quota failure is a blocked experiment, not a design pass.

## 6. Test B — Codex reads file state with both Claude tokens removed

This test proves file-based handoff, **not** Claude transcript import. Codex keeps
its own device login; remove both **Claude** tokens from both process environment
and their runtime directory for the duration of the test. Do not delete them.

1. Stop the heartbeat service temporarily. As root move the two Claude env files
   to a newly created root-only (0700) directory under `/run`. Install a shell
   `trap` to restore both files (hydra:hydra, 0600) and restart the heartbeat on
   completion/error/interruption. Do not display the files; do not leave the
   quarantine owned or readable by hydra. Confirm the two original paths are
   absent and no persistent Claude login exists. If interrupted by reboot, `/run`
   is ephemeral: restore from a separately secured source or repeat setup-token.
2. Create `/srv/hydra/manager/MANAGER-HANDOFF.md` **only if absent**, as root with
   hydra ownership and mode 0600 (the manager parent is deliberately root-owned).
   Minimal nonsecret fixture:

   ```markdown
   # Manager handoff (bootstrap smoke test, not production state)
   Infrastructure is provisioned; the manager is heartbeat-only.
   No factory loop has launched. Next gate: L reviews the two smoke-test results.
   Company state: /srv/hydra/repos/faden/factory/state.json
   ```

3. As hydra create `/srv/hydra/repos/faden/factory/state.json` **only if absent**;
   first create its `factory/` parent if needed. Minimal fixture:

   ```json
   {"schema_version": 1, "smoke_test": true, "manager": "heartbeat-only", "tracks": [], "next_gate": "L reviews manager smoke tests"}
   ```

   Preserve any real existing state verbatim. These are local smoke-test artifacts,
   not an authoritative factory schema or a commit. Record which files were newly
   created; do not accidentally stage them in a later PR.
4. Run as hydra in a **clean environment**, with neither token sourced:

   ```sh
   sudo -u hydra env -i HOME=/home/hydra USER=hydra LOGNAME=hydra \
     PATH=/usr/local/bin:/usr/bin:/bin \
     CLAUDE_CONFIG_DIR=/srv/hydra/manager/.claude \
     codex exec -C /srv/hydra/repos/faden \
       'Read /srv/hydra/manager/MANAGER-HANDOFF.md and /srv/hydra/repos/faden/factory/state.json from disk. State where things stand, what is running, and the next gate. Cite both paths. Do not modify anything or read credential files.'
   ```

5. Capture and post the complete raw Codex stdout/stderr and exit code after a
   local secret check, plus nonsecret evidence that both Claude token files were
   absent and the child environment was clean. Check it actually read **both**
   files and described their real content. A plausible summary without file-read
   evidence is insufficient. Restore the token files and heartbeat via the trap,
   checking ownership/modes and service status. Record failures honestly.

## 7. Provisioning acceptance and stop

As admin, verify without displaying credentials:

```sh
sudo systemctl is-active hydra-manager.service
sudo systemctl is-enabled hydra-manager.service
sudo journalctl -u hydra-manager.service -n 5 --no-pager
sudo stat -c '%U:%G %a %n' /srv/hydra/manager/{.claude,credentials,inbox,logs}
sudo -u hydra id
sudo -l -U hydra # if sudo is installed: hydra must not have a usable allowed command
sudo -u hydra /usr/local/bin/node --version
sudo -u hydra /usr/local/bin/claude --version
sudo -u hydra /usr/local/bin/codex --version
python3.12 --version
gh --version
rclone version
tmux -V
sudo tailscale status
fc-match Inter
fc-match 'Noto Sans'
fc-match 'Noto Color Emoji'
sudo -u hydra git -C /srv/hydra/repos/faden status --short
sudo -u hydra git -C /srv/hydra/repos/hydra-agent status --short
```

Confirm a second heartbeat about five minutes after the first. Rerun bootstrap
and verify exit 0, no token/config loss, no dirty-file loss, unchanged retained
tool versions, no duplicate users/services, and no factory/AI activity. The first
pre-login run's `.claude/` and `credentials/` must be empty; they are deliberately
**not cleared** on reruns after human login.

Local Phase 1 checks (macOS, **not Ubuntu integration**):

```sh
bash -n setup/manager-vm.sh
bash -n setup/gcp-vm.sh
python3 -m unittest discover -s tests -v
# GCP tests intercept gcloud; no actual cloud calls or bootstrap main runs.
# Optional when installed:
shellcheck --severity=style setup/manager-vm.sh setup/gcp-vm.sh
```

Post the provisioning result and both experiments' raw evidence to the original
thread. Stop here. Actual supervisor policy, Slack identity/allowlist, work
launch, additional service users and backups are separate reviewed work.

## 8. Starting the manager (after the `loops/b1` merge, Hermes on the VM)

Everything here is a human or Hermes step over SSH. Tokens go in through the GCP
web SSH or a hidden prompt, never through Slack or a chat transcript.

1. As hydra: `git -C /srv/hydra/repos/hydra-agent pull --ff-only`. As root:
   `sudo /usr/local/sbin/hydra-manager-bootstrap` (rerun; expects
   `MANAGER: installed ...` and exit 0). Check `hydra --help` and
   `sudo -u hydra HYDRA_HOME=/srv/hydra/manager /srv/hydra/manager/venv/bin/python -c 'import slack_bolt'`.
2. L creates the Slack app (`@manager`): Socket Mode on, an app-level token with
   `connections:write`, bot scopes `chat:write`, `channels:history`,
   `groups:history`, `im:history`, `files:read`, `reactions:read`, `users:read`;
   events `message.channels`, `message.groups`, `message.im`, `app_mention`,
   `reaction_added`, `file_shared`; installed to the workspace and invited to `#dev`.
3. As hydra, `umask 077`, write `/srv/hydra/manager/credentials/slack.env` (mode 0600)
   with `SLACK_BOT_TOKEN=...` and `SLACK_APP_TOKEN=...` through a hidden prompt, the
   same way the Claude tokens went in (section 3). Never paste a token on a command line.
4. As hydra write `/srv/hydra/manager/allowlist.json`: the founder's Slack user id with
   `{"instructs": true}`, the operators' bot ids with `{"instructs": false}`; and
   `/srv/hydra/manager/config.json`:

   ```json
   {"repo": "/srv/hydra/repos/faden", "dev_channel": "C_THE_DEV_CHANNEL_ID"}
   ```

   Optional: `budgets.json` (`{"turns_per_hour": 20, "claude_turns_per_day": {"claude-r2d2": 200, "claude-l": 200}}`)
   and `credentials/buildlog.env` with `BUILDLOG_WEBHOOK=...` for the dead-man.
5. Dry checks as hydra, no network to Slack yet:
   `HYDRA_HOME=/srv/hydra/manager python3 /srv/hydra/manager/app/manager/bridge.py --check`
   must print the allowlist counts and exit 0; `hydra status` must render.
6. As root: `systemctl restart hydra-manager.service hydra-bridge.service`, then
   `journalctl -u hydra-bridge -n 20` shows `bridge up as U...`, and
   `journalctl -u hydra-manager -n 20` shows `service loop`.
7. From `#dev`: `@manager status`. Then one shadow cycle: `@manager read the repo and
   post what you would do next`; the manager answers in the thread and rewrites
   `MANAGER-HANDOFF.md`; L confirms; then it is live. `@manager pause` (founder only)
   stops turns at any time; `hydra pause` does the same from the console.

The first Claude turn creates `session-id` (a fresh session; `--resume` afterwards).
Rotation to the L account or Codex happens on quota and is visible as `engine: <acc> (<model>)`
in the thread and in `hydra status`; `hydra engine acc=claude-r2d2 model=fable5.1` (or the
short form `hydra engine claude-r2d2`) moves back. `engine` is JSON `{"acc", "model"}`; a
legacy one-word file is upgraded on the next turn.

After every merge to `manager/` on `main`, as root: `hydra update`. It fast-forwards the
hydra-agent clone, re-copies `manager/` into `app/manager/`, refreshes `CLAUDE.md` and the
`AGENTS.md` link, restarts both services and prints `updated to <commit>`. Without it the
deployed rules and code stay at whatever the bootstrap copied.

Shared memory lives in the faden clone at `factory/manager-memory/` (`MEMORY.md`, `claude/`,
`codex/NOTES.md`, `MANAGER-HANDOFF.md`, `LEDGER.jsonl`); the supervisor commits it after every
turn together with `state.json` and the logs. `$HYDRA_HOME/MANAGER-HANDOFF.md` becomes a
symlink into that folder on the first turn after the update.

