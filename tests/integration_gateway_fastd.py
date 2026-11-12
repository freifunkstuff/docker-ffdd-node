#!/usr/bin/env python3
"""Opt-in real fastd proof, synthetic identities, disposable Docker only.

Run: python3 dockernode/tests/integration_gateway_fastd.py --image ffdd-node:gateway-candidate
Requires local image, Docker and /dev/net/tun. Uses an internal Docker network,
no host networking, no bind mounts and no privileged container. The runner
renders dedicated test configs using the production hook API, not node startup.
Revocation is explicitly followed by a controlled daemon restart: persistence
alone is NOT claimed to terminate an already established fastd connection.
"""
import argparse
import io
from pathlib import Path
import subprocess
import tarfile
import uuid

CAPS = ("NET_ADMIN", "NET_RAW", "NET_BIND_SERVICE", "SETUID", "SETGID", "CHOWN", "DAC_OVERRIDE", "KILL")
RUNNER = r'''
import os, pathlib, subprocess, time, signal, re
from gateway_fastd import GatewayFastd
root = pathlib.Path('/tmp/gateway-proof')
root.mkdir(mode=0o700)
env = dict(os.environ, NODE_ROLE='gateway', GATEWAY_FASTD_ROOT=str(root/'data'),
           GATEWAY_FASTD_CONNECTION_DIR=str(root/'connections'), GATEWAY_FASTD_RESTRICT='false')
(root/'data'/'accepted').mkdir(parents=True)
null = open(os.devnull, 'wb')
processes = []
def identity(name):
    secret = subprocess.check_output(['fastd','--generate-key','--machine-readable'], stderr=null).decode().strip()
    assert len(secret) == 64
    conf = root/(name+'.conf')
    conf.write_text('secret "'+secret+'";\nmode multitap;\nmethod "null";\ndrop capabilities no;\n')
    conf.chmod(0o600)
    key = subprocess.check_output(['fastd','--config',str(conf),'--show-key','--machine-readable'], stderr=null).decode().strip()
    assert len(key) == 64
    return conf, key
server, server_key = identity('server')
client, client_key = identity('client')
unknown, unknown_key = identity('unknown')
base = server.read_text()
server.write_text(base+'bind 127.0.0.1:51913;\ninclude peers from "'+str(root/'data'/'accepted')+'";\n'+''.join(
    'on '+event+' '+('sync ' if event == 'verify' else '')+'"python3 /tmp/gateway_fastd.py '+action+'";\n'
    for event, action in [('verify','verify'),('establish','establish'),('disestablish','disestablish')]))
for conf, prefix in [(client,'clproof'),(unknown,'unproof')]:
    conf.write_text(conf.read_text()+'peer "gateway" { key "'+server_key+'"; remote 127.0.0.1:51913; }\n')
def start(conf):
    log = open(str(conf)+'.log', 'wb')
    p = subprocess.Popen(['fastd','--config',str(conf)],env=env,stdout=log,stderr=log)
    p.proof_log = pathlib.Path(str(conf)+'.log')
    log.close()
    processes.append(p)
    return p
def stop(p):
    if p.poll() is None:
        p.send_signal(signal.SIGTERM)
        try: p.wait(timeout=10)
        except subprocess.TimeoutExpired: p.kill(); p.wait(timeout=5)
def wait_for(predicate, description, seconds=30):
    deadline = time.monotonic()+seconds
    while time.monotonic()<deadline:
        if predicate(): return
        if any(p.poll() is not None for p in processes):
            # Stopped processes are removed by callers before this check.
            raise AssertionError('daemon exited during '+description)
        time.sleep(.1)
    raise AssertionError('timeout: '+description)
def restart(p):
    stop(p); processes.remove(p)
    GatewayFastd(env).reset()
    return start(server)
gw = GatewayFastd(env)
marker = gw.connections/client_key
try:
    daemon = start(server)
    peer = start(client)
    wait_for(marker.exists, 'unknown incoming admission and establish')
    assert gw._config(client_key).exists()
    assert gw.counts()['connected'] == 1
    print('PASS real incoming unknown admission and established marker', flush=True)
    stop(peer); processes.remove(peer)
    wait_for(lambda: not marker.exists(), 'disestablish marker cleanup', seconds=120)
    env['GATEWAY_FASTD_RESTRICT'] = 'true'
    gw = GatewayFastd(env)
    daemon = restart(daemon)
    peer = start(client)
    wait_for(marker.exists, 'persisted known key after restricted restart')
    stranger = start(unknown)
    # Allow handshake/retry time; verify no persistent or connection admission.
    deadline = time.monotonic()+8
    while time.monotonic()<deadline:
        assert not (gw.connections/unknown_key).exists()
        assert not gw._config(unknown_key).exists()
        assert daemon.poll() is None and stranger.poll() is None
        time.sleep(.1)
    stop(stranger); processes.remove(stranger)
    print('PASS restart preserves known key; restricted unknown denied', flush=True)
    gw.revoke(client_key)
    assert marker.exists(), 'persistent revocation must not fake disestablishment'
    assert gw.counts() == {'accepted': 0, 'connected': 1}
    # Observe the actual v23 reload behavior, without assuming HUP disconnects.
    daemon.send_signal(signal.SIGHUP)
    deadline = time.monotonic()+2
    while time.monotonic()<deadline:
        assert daemon.poll() is None
        time.sleep(.1)
    print('INFO real HUP revoked connection marker '+('remains' if marker.exists() else 'cleared')+'; explicit restart follows', flush=True)
    daemon = restart(daemon)
    assert not marker.exists()
    env['GATEWAY_FASTD_RESTRICT'] = 'false'
    daemon = restart(daemon)
    # Force a fresh handshake instead of relying on established-client timeout.
    stop(peer); processes.remove(peer)
    peer = start(client)
    deadline = time.monotonic()+8
    while time.monotonic()<deadline:
        assert not marker.exists()
        assert not gw._config(client_key).exists()
        assert daemon.poll() is None and peer.poll() is None
        time.sleep(.1)
    assert gw._state()['records'][client_key]['revoked']
    print('PASS explicit operator restart terminates revoked session; open reenrollment denied', flush=True)
finally:
    for p in reversed(processes): stop(p)
    for p in processes:
        text = re.sub(r'[0-9a-fA-F]{64}', '[REDACTED]', p.proof_log.read_text(errors='replace'))
        print('DIAGNOSTIC '+p.proof_log.name+' '+text[-6000:], flush=True)
'''


def docker(*args, **kwargs):
    return subprocess.run(["docker", *args], check=True, timeout=kwargs.pop("timeout", 60), **kwargs)


def archive(files):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as tar:
        for name, data in files.items():
            data = data.encode()
            item = tarfile.TarInfo(name)
            item.size = len(data)
            item.mode = 0o600
            tar.addfile(item, io.BytesIO(data))
    return output.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="ffdd-node:gateway-candidate")
    args = parser.parse_args()
    name = "ffdd-gateway-proof-" + uuid.uuid4().hex
    container = None
    network_created = False
    try:
        docker("network", "create", "--internal", "--label", "ffdd.gateway-proof="+name, name, capture_output=True)
        network_created = True
        caps = [item for cap in CAPS for item in ("--cap-add", cap)]
        container = docker("create", "--network", name, "--label", "ffdd.gateway-proof="+name,
                           "--cap-drop", "ALL", *caps, "--device", "/dev/net/tun:/dev/net/tun",
                           "--entrypoint", "sh", args.image, "-c", "while :; do sleep 3600; done",
                           capture_output=True, text=True).stdout.strip()
        docker("start", container, capture_output=True)
        helper = Path(__file__).resolve().parents[1]/"scripts/gateway_fastd.py"
        docker("cp", "-", container+":/tmp", input=archive({"runner.py": RUNNER, "gateway_fastd.py": helper.read_text()}))
        docker("exec", container, "python3", "/tmp/runner.py", timeout=240)
    finally:
        if container:
            docker("rm", "-f", "-v", container, capture_output=True)
        if network_created:
            docker("network", "rm", name, capture_output=True)


if __name__ == "__main__":
    main()
