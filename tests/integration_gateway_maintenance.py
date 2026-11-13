#!/usr/bin/env python3
"""Real 143-peer maintenance proof; opt-in, isolated, not a full exit probe.

Run with --image ffdd-node:enrollment-review after the candidate build completes.
Only synthetic identities generated inside the disposable network-none container
are used. No host mounts, devices, production connectivity or private-key output.
"""
import argparse
import json
import subprocess
import uuid

CAPS = ('NET_ADMIN', 'NET_RAW', 'NET_BIND_SERVICE', 'SETUID', 'SETGID',
        'CHOWN', 'DAC_OVERRIDE', 'KILL')
LABEL = 'ffdd.gateway-maintenance-proof'

# Executed inside the candidate: imports and all mutations use the actual image.
PROOF = r'''
import json, os, pathlib, subprocess, sys, threading, time
os.environ['NODE_ROLE'] = 'gateway'
sys.path.insert(0, '/usr/local/bin')
import gateway_runtime as runtime
from gateway_health import fresh_ready, runtime_ready
from gateway_control import AnnouncementController
from sysinfo import _read_interface_snapshot
from gateway_api import GatewayApplication, make_server
from gateway_store import GatewayStore
from urllib.parse import urlencode
from urllib.request import urlopen
root = pathlib.Path('/tmp/maintenance-proof'); root.mkdir(mode=0o700)
def run(*args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, **kwargs)
def key():
    secret = run('wg', 'genkey').stdout
    public = run('wg', 'pubkey', input=secret).stdout.decode().strip()
    return secret.decode().strip(), public
secret, server_public = key()
incoming = [dict(node=n, publickey=key()[1]) for n in range(1000, 1143)]
primary = runtime.node_addresses(901)['primary_ip']
run('ip', 'link', 'set', 'lo', 'up')
run('ip', 'link', 'add', 'bmxproof', 'type', 'bridge')
run('ip', 'addr', 'add', primary+'/32', 'dev', 'bmxproof')
run('ip', 'link', 'set', 'bmxproof', 'up')
env = root/'bmxd.env'
env.write_text('BMXD_PRIMARY_IP="'+primary+'"\nBMXD_BACKBONE_INTERFACES=""\n')
log = (root/'bmxd.log').open('wb')
daemon = subprocess.Popen(['bmxd', '--no_fork', '0', '--dev=bmxproof',
                           '/linklayer', '0', '-r', '3'], stdout=log, stderr=log)
stop = threading.Event(); history = []; calls = []; failures = []; phase = ['startup']
def controller_run(args, **kwargs):
    calls.append(list(args))
    return subprocess.run(args, **kwargs)
controller = AnnouncementController(controller_run)
class DelayedRunner(runtime.CommandRunner):
    delay = 0
    during_delay = None
    def run(self, *args, **kwargs):
        if 'private-key' in args and self.delay:
            delay, self.delay = self.delay, 0
            start = time.monotonic()
            if self.during_delay:
                callback, self.during_delay = self.during_delay, None
                time.sleep(.1)
                callback()
            time.sleep(max(0, delay-(time.monotonic()-start)))
        return super().run(*args, **kwargs)
runner = DelayedRunner()
owner = runtime.RuntimeOwner(runner, root/'runtime', env, root/'public.json')
status_path = root/'runtime/runtime.json'
def status():
    return json.loads(status_path.read_text())
store = GatewayStore(root/'store', max_peers=512)
for peer in incoming:
    store.enroll(peer['node'], peer['publickey'], 901)
identity = root/'identity.json'
runtime.atomic_json(identity, dict(node=901, key=server_public, port=5003))
outgoing = root/'outgoing.json'
runtime.atomic_json(outgoing, {'peers': []})
application = GatewayApplication(store=store, role='gateway', identity_file=identity,
    runtime_file=status_path, outgoing_file=outgoing, wait_seconds=.1)
http_server = make_server(application, port=0)
http_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
http_thread.start()
http_results = []
unknown_public = key()[1]
def http_get(node=None, public=None):
    query = '' if node is None else '?'+urlencode(dict(node=node, key=public))
    with urlopen('http://127.0.0.1:'+str(http_server.server_port)+'/wg.cgi'+query, timeout=2) as response:
        assert response.status == 200
        return json.load(response)
def repair_http_probe():
    assert status()['state'] == 'repairing' and status()['peers'] == {}
    discovery = http_get()
    assert discovery['status']=='NotRestricted' and discovery['server']==dict(node=901, key=server_public, port=5003)
    known = http_get(1000, incoming[0]['publickey'])
    assert known['status']=='RequestAlreadyRegistered' and known['client']==dict(node=1000, key=incoming[0]['publickey'])
    first = http_get(2000, unknown_public)
    assert first['status']=='RequestFailed' and 'client' not in first
    assert any(p['node']==2000 for p in store.snapshot()), 'new intent was not stored'
    repeat = http_get(2000, unknown_public)
    assert repeat['status']=='RequestFailed' and 'client' not in repeat
    assert status()['state']=='repairing', 'HTTP probes escaped repair window'
    http_results.append(dict(phase=phase[0], discovery=discovery['status'],
        known=known['status'], unknown=first['status'], storedUnapplied=repeat['status']))
backbone_history = []
backbone_members = {}
def backbone_snapshot(state):
    counters, inventory = _read_interface_snapshot()
    totals = {}
    for direction in ('rx', 'tx'):
        name = 'tbb_wg_' + direction
        assert name in counters and len(inventory[name]) == 143, 'backbone inventory disappeared'
        if name in backbone_members:
            assert inventory[name] == backbone_members[name], 'backbone membership changed'
        else:
            backbone_members[name] = inventory[name]
        totals[direction] = int(counters[name])
        assert totals[direction] >= 0
        if backbone_history:
            assert totals[direction] >= backbone_history[-1]['totals'][direction], 'backbone counter regressed'
    backbone_history.append(dict(phase=phase[0], state=state, totals=totals))
def observe():
    last_backbone = 0
    while not stop.is_set():
        try:
            record = status()
            ready = runtime_ready(record)
            if record['state'] == 'repairing':
                assert record['peers'] == {} and not fresh_ready(record)
                assert not runtime_ready(dict(record), now=record['timestamp']+11)
            # Sysfs aggregation is intentionally sampled at ~5Hz, not every
            # readiness tick. Preserve all 143 real members while API peers={}.
            if phase[0].startswith('repair') and time.monotonic()-last_backbone >= .2:
                backbone_snapshot(record['state'])
                last_backbone = time.monotonic()
            # Other health inputs are explicitly controlled, NOT real exit probes.
            checks = dict(exit=True, admission=True, dns=True, peers=ready)
            assert controller.apply(all(checks.values()), True), 'controller command failed'
            history.append(dict(phase=phase[0], state=record['state'],
                                peers=len(record['peers']), fresh=fresh_ready(record),
                                service=ready, advertised=controller.advertised))
        except FileNotFoundError:
            pass
        except Exception:
            failures.append('observer assertion failed'); stop.set()
        stop.wait(.05)
def wait_for(predicate, label):
    deadline = time.monotonic()+3
    while time.monotonic() < deadline:
        assert not failures, failures
        if predicate(): return
        time.sleep(.02)
    raise AssertionError('timeout: '+label)
def options(advertised):
    text = run('bmxd', '-c', '--options').stdout.decode()
    lines = [line for line in text.splitlines() if 'gateway_class' in line]
    assert bool(lines) == advertised, 'unexpected actual gateway_class presence'
    if advertised:
        assert any('8MBit' in line for line in lines), 'unexpected gateway bandwidth'
def apply():
    return owner.reconcile(901, secret, [], incoming)
thread = threading.Thread(target=observe, daemon=True)
try:
    # Let the daemon establish its default runtime directory before clients race
    # its initial filesystem setup (older bmxd exits on concurrent creation).
    time.sleep(.2)
    deadline = time.monotonic()+5
    while True:
        p = subprocess.run(['bmxd', '-c', '--options'], capture_output=True)
        if p.returncode == 0: break
        assert time.monotonic() < deadline, 'bmxd control unavailable'
        time.sleep(.05)
    thread.start()
    runner.delay = .2
    apply()
    wait_for(lambda: controller.advertised, 'initial announcement')
    assert any(h['state']=='applying' and not h['service'] for h in history)
    assert status()['state']=='ready' and len(status()['peers'])==143
    options(True)
    # Initial fail-closed withdrawal is separate from post-ready call assertions.
    first_ready_calls = len(calls)
    assert sum('-g' in a for a in calls)==1
    backbone_snapshot('ready')
    durations = []
    for cycle in range(2):
        phase[0] = 'repair'+str(cycle+1)
        owner._last_apply -= 31
        runner.delay = 3
        runner.during_delay = repair_http_probe
        start = time.monotonic(); apply(); durations.append(time.monotonic()-start)
        time.sleep(.1)
        rows = [h for h in history if h['phase']==phase[0]]
        repairing = [h for h in rows if h['state']=='repairing']
        assert len(repairing)>=20, 'observer did not sample repair window'
        assert all(h['peers']==0 and not h['fresh'] and h['service'] and h['advertised'] for h in repairing)
        assert all(h['service'] and h['advertised'] for h in rows), 'false unhealthy during maintenance'
        assert sum(h['phase']==phase[0] and h['state']=='repairing' for h in backbone_history)>=10, 'insufficient real backbone repair samples'
        assert not failures, failures
        options(True)
    assert len(http_results)==2, 'both repairs require real HTTP admission probes'
    assert sum(durations)/2 < 10, 'maintenance exceeded bound'
    assert len(calls)==first_ready_calls, 'maintenance changed announcement'
    phase[0] = 'invalidate'; owner.invalidate()
    wait_for(lambda: not controller.advertised, 'invalidate withdrawal')
    assert status()['state']=='retrying' and not runtime_ready(status())
    assert len(calls)==first_ready_calls+1 and calls[-1][-2:]==['-r','3']
    options(False)
    phase[0] = 'recover'; apply()
    wait_for(lambda: controller.advertised, 'recovery announcement')
    # Real kernel drift must deny the lease, even for unchanged desired intent.
    run('wg', 'set', 'tbbwg', 'peer', incoming[0]['publickey'], 'remove')
    phase[0] = 'drift'; owner._last_apply -= 31; runner.delay = .4
    apply()
    wait_for(lambda: controller.advertised, 'drift repaired announcement')
    rows = [h for h in history if h['phase']=='drift' and h['state']=='applying']
    assert len(rows)>=2 and all(not h['service'] and not h['fresh'] and h['peers']==0 and not h['advertised'] for h in rows)
    assert sum('-g' in a for a in calls)==3
    assert sum('-r' in a for a in calls[first_ready_calls:])==2
    options(True)
    final = status()
    assert final['state']=='ready' and len(final['peers'])==143
    assert len(run('wg','show','tbbwg','peers').stdout.split())==143
    links = json.loads(run('ip','-d','-j','link','show').stdout)
    assert sum(link.get('ifalias','').startswith(runtime.ALIAS) for link in links)==143
    assert not failures
    print(json.dumps(dict(result='PASS', peers=143, repairs=2,
        repairSeconds=[round(n,3) for n in durations], observerSamples=len(history),
        observedStates=sorted(set(h['state'] for h in history)),
        snapshotCounts={state: sum(h['state']==state for h in history)
                        for state in sorted(set(h['state'] for h in history))},
        kernelWireGuardPeers=143, ownedIPIPInterfaces=143,
        fixedBackboneMembership143AtRepair=True,
        backboneSnapshotCount=len(backbone_history),
        backboneByteTotalsFirst=backbone_history[0]['totals'],
        backboneByteTotalsLast=backbone_history[-1]['totals'],
        backboneBytesMonotonic=True, repairHTTPResults=http_results,
        announcementCalls=calls, scope='real RuntimeOwner, kernel WG/IPIP, bmxd and AnnouncementController; no fake provider',
        otherHealthInputs='controlled true; not full exit probe',
        isolation='network none; no mounts/devices; cap-drop ALL + eight caps')))
except Exception as error:
    import traceback
    print(json.dumps(dict(result='FAIL', error=type(error).__name__,
                          lines=[frame.lineno for frame in traceback.extract_tb(error.__traceback__)])))
    raise SystemExit(1)
finally:
    stop.set()
    if thread.ident: thread.join(timeout=3)
    http_server.shutdown(); http_server.server_close(); http_thread.join(timeout=3)
    daemon.terminate()
    try: daemon.wait(timeout=3)
    except subprocess.TimeoutExpired: daemon.kill(); daemon.wait()
    log.close()
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', default='ffdd-node:enrollment-review')
    args = parser.parse_args()
    name = 'ffdd-maintenance-' + uuid.uuid4().hex[:16]
    caps = [arg for cap in CAPS for arg in ('--cap-add', cap)]
    command = ['docker', 'run', '--rm', '--name', name, '--label', LABEL+'='+name,
               '--network', 'none', '--cap-drop', 'ALL', *caps,
               '--security-opt', 'no-new-privileges', '--entrypoint', 'python3',
               args.image, '-c', PROOF]
    try:
        result = subprocess.run(command, capture_output=True, timeout=90)
        if result.returncode:
            # Never echo subprocess diagnostics: a failing tool could include keys.
            try:
                diagnostic = json.loads(result.stdout)
                print(json.dumps({k: diagnostic[k] for k in ('result', 'error', 'lines') if k in diagnostic}))
            except (ValueError, TypeError):
                pass
            raise SystemExit('FAIL: isolated proof failed (container diagnostics suppressed)')
        print(json.dumps(json.loads(result.stdout), indent=2))
    finally:
        # Name is generated here, not user input; verify ownership before removal.
        inspected = subprocess.run(['docker', 'inspect', name], capture_output=True)
        if inspected.returncode == 0:
            info = json.loads(inspected.stdout)[0]
            if info.get('Config', {}).get('Labels', {}).get(LABEL) != name:
                raise RuntimeError('refusing cleanup of unowned container')
            subprocess.run(['docker', 'rm', '-f', name], check=True, capture_output=True)


if __name__ == '__main__':
    main()
