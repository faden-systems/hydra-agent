#!/usr/bin/env python3
"""Run one VM command as hydra (default) or the authorized root operator.
No retries; verified SSH host identity; credentials never embedded.
Usage: hydra_vm_cmd.py [--user hydra|root] [--timeout SECONDS] 'command'
"""
import argparse
import pathlib
import shlex
import subprocess


def ssh_args():
    home = pathlib.Path.home()
    return ['ssh', '-i', str(home / '.ssh/id_ed25519'),
            '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'HostKeyAlias=compute.6444324875612010050',
            '-o', 'UserKnownHostsFile=' + str(home / '.ssh/google_compute_known_hosts'),
            '-o', 'ConnectTimeout=15', 'hermes@35.199.150.160']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user', choices=['hydra', 'root'], default='hydra')
    parser.add_argument('--timeout', type=float, default=180)
    parser.add_argument('command')
    args = parser.parse_args()
    if args.user == 'hydra':
        remote = 'sudo -n -iu hydra bash -lc ' + shlex.quote(args.command)
    else:
        remote = 'sudo -n bash -lc ' + shlex.quote(args.command)
    try:
        return subprocess.run(ssh_args() + [remote], timeout=args.timeout).returncode
    except subprocess.TimeoutExpired:
        parser.exit(124, 'SSH timed out; remote command may still be running. Inspect before retrying.\n')


if __name__ == '__main__':
    raise SystemExit(main())
