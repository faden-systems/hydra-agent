#!/usr/bin/env python3
"""Exit-owned transparent real-CLI recorder. Never supplies fake replies or logs environment."""
import hashlib,json,os,signal,subprocess,sys,threading,time
from pathlib import Path

def process_start(pid):
    return (Path('/proc')/str(pid)/'stat').read_text().rsplit(')',1)[1].split()[19]

def main():
    binary=Path(os.environ['B8_REAL_CLAUDE']).resolve(strict=True)
    root=Path(os.environ['B8_CAPTURE_WIRE_DIR']).resolve(strict=True)
    if not root.is_dir() or not binary.is_file():raise SystemExit('invalid capture paths')
    os.umask(0o077)
    prefix=root/str(os.getpid())
    # All tests pass only CLI switches/model/session ids in argv; never accept secret switches.
    args=sys.argv[1:]
    if any('token' in a.lower() or 'api-key' in a.lower() for a in args):
        raise SystemExit('credential arguments prohibited')
    secrets=[os.environ[k].encode() for k in ('CLAUDE_CODE_OAUTH_TOKEN','ANTHROPIC_API_KEY')
             if os.environ.get(k)]
    binary_digest=hashlib.sha256(binary.read_bytes()).hexdigest()
    child=subprocess.Popen([str(binary),*args],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE,start_new_session=True)
    meta={'wrapper_pid':os.getpid(),'child_pid':child.pid,'binary_path':str(binary),
          'binary_sha256':binary_digest,
          'wrapper_start':process_start(os.getpid()),'child_start':process_start(child.pid),
          'argv':args,'started_at':time.time()}
    metadata=Path(str(prefix)+'.json')
    metadata.write_text(json.dumps(meta)+'\n')
    def forward_signal(signum,_):
        try:os.killpg(child.pid,signum)
        except ProcessLookupError:pass
    for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP):signal.signal(sig,forward_signal)
    def input_pipe():
        try:
            while True:
                chunk=os.read(sys.stdin.fileno(),65536)
                if not chunk:break
                child.stdin.write(chunk);child.stdin.flush()
        except (BrokenPipeError,OSError):pass
        finally:
            try:child.stdin.close()
            except (BrokenPipeError,OSError):pass
    def output_pipe(source,target,suffix):
        # Line buffering keeps a credential split across OS reads out of retained evidence.
        with open(str(prefix)+suffix,'xb') as evidence:
            for line in iter(source.readline,b''):
                retained=line
                for secret in secrets:retained=retained.replace(secret,b'[REDACTED]')
                evidence.write(retained);evidence.flush()
                target.write(line);target.flush()
    readers=[threading.Thread(target=output_pipe,args=(child.stdout,sys.stdout.buffer,'.stdout'),daemon=True),
             threading.Thread(target=output_pipe,args=(child.stderr,sys.stderr.buffer,'.stderr'),daemon=True)]
    threading.Thread(target=input_pipe,daemon=True).start()
    for reader in readers:reader.start()
    rc=child.wait()
    for reader in readers:reader.join(timeout=3)
    if any(r.is_alive() for r in readers):
        try:os.killpg(child.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        raise SystemExit('capture output pipes did not close')
    meta.update(returncode=rc,ended_at=time.time())
    metadata.write_text(json.dumps(meta)+'\n')
    return rc if rc>=0 else 128-rc

if __name__=='__main__':sys.exit(main())
