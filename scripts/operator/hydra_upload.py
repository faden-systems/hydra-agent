#!/usr/bin/env python3
"""Upload a file as hydra, refusing overwrite; optionally replace atomically.
Usage: hydra_upload.py [--replace] LOCAL ABSOLUTE_REMOTE
Remote parent must exist. No retries. Does not upload or discover credentials.
"""
import argparse
import hashlib
import pathlib
import shlex
import subprocess
from hydra_vm_cmd import ssh_args


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--replace', action='store_true')
    parser.add_argument('local', type=pathlib.Path)
    parser.add_argument('remote')
    args = parser.parse_args()
    if not args.remote.startswith('/'):
        parser.error('remote path must be absolute')
    data = args.local.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    code = '''import os,sys,pathlib,tempfile,hashlib
os.umask(0o077)
p=pathlib.Path(sys.argv[1]);replace=sys.argv[2]=='True';expected=sys.argv[3]
assert not p.is_symlink(), 'destination is a symlink'
b=sys.stdin.buffer.read();assert hashlib.sha256(b).hexdigest()==expected
fd,t=tempfile.mkstemp(prefix='.upload-',dir=p.parent)
try:
 with os.fdopen(fd,'wb') as f:f.write(b);f.flush();os.fsync(f.fileno())
 if replace:os.replace(t,p)
 else:os.link(t,p)
 assert hashlib.sha256(p.read_bytes()).hexdigest()==expected
 print('verified upload:',str(p),'bytes=',len(b))
finally:
 if os.path.exists(t):os.unlink(t)
'''
    remote = 'sudo -n -iu hydra ' + shlex.join(['python3', '-c', 'exec(' + repr(code) + ')', args.remote, str(args.replace), digest])
    try:
        return subprocess.run(ssh_args() + [remote], input=data, timeout=60).returncode
    except subprocess.TimeoutExpired:
        parser.exit(124, 'Upload timed out; inspect destination before retrying.\n')


if __name__ == '__main__':
    raise SystemExit(main())
