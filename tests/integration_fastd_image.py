#!/usr/bin/env python3
"""Opt-in actual-image proof; no real keys, host mounts, network or privileges.

python3 dockernode/tests/integration_fastd_image.py --image IMAGE
Requires a cached Alpine compiler image (override --builder). All payloads and
synthetic secrets live in uniquely labelled throwaway containers. No image is
built or altered. Multitap with no peers exercises real daemon hooks without TUN.
"""
import argparse
import io
import json
import subprocess
import tarfile
import uuid

SHIM = r'''
#define _GNU_SOURCE
#define syscall hidden_syscall_declaration
#include <unistd.h>
#undef syscall
#include <sys/syscall.h>
#include <dlfcn.h>
#include <errno.h>
#include <stdlib.h>
#include <fcntl.h>
static long (*real_syscall)(long,...);
__attribute__((constructor)) static void init(void) {
    real_syscall = dlsym(RTLD_NEXT, "syscall");
    if (!real_syscall) _exit(98);
}
/* Linux syscall ABI has at most six integer/pointer arguments. The fixed
 * signature preserves all six registers when forwarding unrelated syscalls. */
long syscall(long nr, unsigned long a, unsigned long b, unsigned long c,
             unsigned long d, unsigned long e, unsigned long f) {
    const char *mode = getenv("PROOF_FORCE");
    if (nr == SYS_close_range && mode && *mode) {
        int fd = open("/tmp/forced", O_WRONLY|O_CREAT|O_APPEND, 0600);
        if (fd >= 0) { write(fd, "x", 1); close(fd); }
        errno = *mode == 'E' ? ENOSYS : EPERM;
        return -1;
    }
    return real_syscall(nr,a,b,c,d,e,f);
}
__attribute__((destructor)) static void audit(void) {
    const char *check = getenv("PROOF_CLI");
    if (!check) return;
    int low = atoi(getenv("PROOF_LOW")), high = atoi(getenv("PROOF_HIGH"));
    if (fcntl(low,F_GETFD) != -1 || errno != EBADF) _exit(96);
    if (fcntl(high,F_GETFD) != -1 || errno != EBADF) _exit(96);
    for (int i=0;i<3;i++) if (fcntl(i,F_GETFD)<0) _exit(97);
    int fd = open("/tmp/audit",O_WRONLY|O_CREAT|O_APPEND,0600);
    if (fd < 0 || write(fd,"x",1)!=1) _exit(99);
    close(fd);
}
'''

HOOK = r'''
import os,json
# Listing /proc adds a transient directory fd: it must already be closed when
# tested, unlike any leaked daemon/socket/sentinel fd.
fds = [int(x) for x in os.listdir('/proc/self/fd')]
assert all(not os.path.exists('/proc/self/fd/'+str(x)) for x in fds if x > 2)
actual = [[os.fstat(i).st_dev,os.fstat(i).st_ino,os.fstat(i).st_mode] for i in range(3)]
assert actual == json.loads(os.environ['PROOF_STDIO'])
with open('/tmp/hooks','a') as f: f.write('PASS\n')
'''

RUNNER = r'''
import os,subprocess,resource,fcntl,json,time,signal,pathlib
binary='/usr/local/bin/fastd'
limit=resource.getrlimit(resource.RLIMIT_NOFILE)[0]
assert limit >= 1048576, limit
initial=os.open('/dev/null',os.O_RDONLY)
low=fcntl.fcntl(initial,fcntl.F_DUPFD,12345)
os.close(initial)
# Bound fd-table memory even when nr_open is ~2^31; keep RLIMIT at its ceiling.
high=fcntl.fcntl(low,fcntl.F_DUPFD,min(limit-16,1000000))
# No stdout/stderr pipes: hook must preserve exactly these three inherited fds.
env=dict(os.environ,LD_PRELOAD='/tmp/proof.so',PROOF_LOW=str(low),PROOF_HIGH=str(high))
env['PROOF_STDIO']=json.dumps([[os.fstat(i).st_dev,os.fstat(i).st_ino,os.fstat(i).st_mode] for i in range(3)])
def invoke(argv, **kw):
    return subprocess.run([binary]+argv,env=env,pass_fds=(low,high),timeout=10,**kw)
for mode in ('','ENOSYS','PERM'):
    env['PROOF_FORCE']=mode
    env['PROOF_CLI']='1'
    pathlib.Path('/tmp/audit').unlink(missing_ok=True)
    # Version output isn't sensitive; suppress it to keep stdout PASS-only.
    with open('/dev/null','wb') as null:
        version=invoke(['--version'],stdout=null,stderr=null)
        assert version.returncode == 0, ('version return code',version.returncode)
        # Capture synthetic key material in memory only; never print it.
        key=invoke(['--generate-key','--machine-readable'],stdout=subprocess.PIPE,stderr=null)
        assert key.returncode == 0
        secret=key.stdout.decode().strip().splitlines()[0]
        assert len(secret)==64 and all(c in '0123456789abcdefABCDEF' for c in secret)
        pathlib.Path('/tmp/fastd.conf').write_text('secret "'+secret+'";\nmode multitap;\nmethod "null";\n')
        os.chmod('/tmp/fastd.conf',0o600)
        derived=invoke(['--config','/tmp/fastd.conf','--show-key','--machine-readable'],stdout=subprocess.PIPE,stderr=null)
        assert derived.returncode == 0 and len(derived.stdout.strip())==64
    assert pathlib.Path('/tmp/audit').read_text() == 'xxx'
    del env['PROOF_CLI']
    pathlib.Path('/tmp/hooks').unlink(missing_ok=True)
    pathlib.Path('/tmp/forced').unlink(missing_ok=True)
    pathlib.Path('/tmp/peers').mkdir(exist_ok=True)
    with open('/tmp/fastd.conf','a') as conf: conf.write('include peers from "/tmp/peers";\ndrop capabilities no;\n')
    command='python3 /tmp/hook.py'
    p=subprocess.Popen([binary,'--config','/tmp/fastd.conf','--on-pre-up',command,'--on-up',command,'--on-down',command,'--on-post-down',command],env=env,pass_fds=(low,high))
    try:
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            if p.poll() is not None: raise AssertionError('daemon exited before hooks')
            if pathlib.Path('/tmp/hooks').exists() and len(pathlib.Path('/tmp/hooks').read_text().splitlines())>=1: break
            time.sleep(.05)
        else: raise AssertionError(('daemon startup hooks timed out',pathlib.Path('/tmp/hooks').read_text() if pathlib.Path('/tmp/hooks').exists() else 'none'))
        assert not pathlib.Path('/proc/'+str(p.pid)+'/fd/'+str(low)).exists()
        assert not pathlib.Path('/proc/'+str(p.pid)+'/fd/'+str(high)).exists()
        p.send_signal(signal.SIGTERM)
        assert p.wait(timeout=10) in (0, -signal.SIGTERM)
        assert pathlib.Path('/tmp/hooks').read_text().splitlines()==['PASS']*2
        if mode: assert pathlib.Path('/tmp/forced').stat().st_size >= 3
    finally:
        if p.poll() is None: p.kill(); p.wait()
    print('PASS actual-image CLI and daemon pre-up/post-down hooks '+(mode or 'native')+' rlimit='+str(limit)+' high_fd='+str(high),flush=True)
os.close(low);os.close(high)
'''


def docker(*args, **kwargs):
    return subprocess.run(['docker', *args], check=True, timeout=kwargs.pop('timeout', 60), **kwargs)


def archive(files):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w') as tar:
        for name, data in files.items():
            data = data.encode() if isinstance(data, str) else data
            item = tarfile.TarInfo(name)
            item.size = len(data)
            item.mode = 0o600
            tar.addfile(item, io.BytesIO(data))
    return output.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--builder', default='ffdd-node-test-builder')
    args = parser.parse_args()
    # Read only the kernel's advertised ceiling; do not change host sysctls.
    with open('/proc/sys/fs/nr_open') as f:
        limit = int(f.read().strip())
    label = 'ffdd.fastd-image-proof=' + uuid.uuid4().hex
    containers = []
    try:
        for image in (args.builder, args.image):
            result = docker('create', '--network', 'none', '--label', label,
                            '--ulimit', f'nofile={limit}:{limit}', '--entrypoint', 'sh', image,
                            '-c', 'while :; do sleep 3600; done', capture_output=True, text=True)
            containers.append(result.stdout.strip())
            docker('start', containers[-1], capture_output=True)
        builder, target = containers
        docker('cp', '-', builder+':/tmp', input=archive({'shim.c': SHIM}))
        docker('exec', builder, 'cc', '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror',
               '-o', '/tmp/proof.so', '/tmp/shim.c', '-ldl')
        compiled = docker('cp', builder+':/tmp/proof.so', '-', capture_output=True).stdout
        with tarfile.open(fileobj=io.BytesIO(compiled)) as tar:
            library = tar.extractfile(tar.getmembers()[0]).read()
        docker('cp', '-', target+':/tmp', input=archive({'proof.so': library, 'hook.py': HOOK, 'runner.py': RUNNER}))
        # Docker exec supplies stable stdin/stdout/stderr. No production entrypoint.
        docker('exec', target, 'python3', '/tmp/runner.py', timeout=120)
    finally:
        for container in reversed(containers):
            # Container IDs came directly from docker create; -v cleans anonymous volumes.
            subprocess.run(['docker','rm','-f','-v',container],stdout=subprocess.DEVNULL,check=True,timeout=30)


if __name__ == '__main__':
    main()
