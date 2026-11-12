"""IPv4 exit controller; only touches explicitly owned links, tables and chains.

An injected Runner makes plans testable without network privileges. No command output
or stderr is ever included in an exception. Tables 52000..52063 and priorities 100..163 are reserved (before BMXD 500 and MAIN).
"""
from __future__ import annotations
import hashlib
import ipaddress
import json
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from exit_config import ExitConfig

PREFIX = 'ffdd-exit:'
FORWARD = 'FFDD_EXIT_FORWARD'
INPUT = 'FFDD_EXIT_INPUT'
OUTPUT = 'FFDD_EXIT_OUTPUT'
NAT = 'FFDD_EXIT_NAT'
DENIED = ('0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8', '169.254.0.0/16', '172.16.0.0/12', '192.0.0.0/24', '192.0.2.0/24', '192.168.0.0/16', '198.18.0.0/15', '198.51.100.0/24', '203.0.113.0/24', '224.0.0.0/3')

class RuntimeErrorSafe(RuntimeError):
    pass

class Runner:
    def run(self, args):
        try:
            return subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True).stdout
        except Exception:
            raise RuntimeErrorSafe('exit network operation failed') from None

def remove_temporary(path, prefix, directory=None):
    target = Path(path)
    parent = Path(directory or tempfile.gettempdir()).resolve()
    if target.resolve().parent != parent or not target.name.startswith(prefix):
        raise RuntimeErrorSafe('temporary file ownership check failed')
    target.unlink()

def interface_name(name):
    return 'wgx' + hashlib.sha256(name.encode()).hexdigest()[:10]

class Controller:
    def __init__(self, node_ip, mesh_sources=(), runner=None, mesh_interfaces=()):
        self.node_ip = str(ipaddress.IPv4Address(node_ip))
        self.mesh_sources = tuple(str(ipaddress.IPv4Network(n)) for n in mesh_sources)
        self.mesh_interfaces = tuple(mesh_interfaces) + ('tbb+',)
        if any(not isinstance(i, str) or not i or len(i) > 15 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-+' for c in i) for i in self.mesh_interfaces):
            raise RuntimeErrorSafe('invalid mesh interface')
        self.runner = runner or Runner()
        self.forward_plan = []
        self.input_plan = []
        self.discovered = False
        self.enabled = False
        self.links = set()
        self.rules = []
        self.tables = set()
        self.sources = set()
        self.status = {'phase': 'disabled', 'exits': []}

    def run(self, *args):
        return self.runner.run(list(args))

    def fw(self, chain, *args, table='filter'):
        if chain == INPUT and table == 'filter':
            self.input_plan.append(args)
            return ''
        if chain == FORWARD and table == 'filter':
            self.forward_plan.append(args)
        return self.run('iptables', '-w', '-t', table, '-A', chain, *args)

    def _gateway_dns_permissions(self):
        if os.environ.get('NODE_ROLE', 'node') != 'gateway':
            return ''
        return ''.join('-A ' + INPUT + ' -i ' + interface + ' -s ' + source + ' -d ' + self.node_ip + ' -p ' + protocol + ' --dport 53 -j ACCEPT' + chr(10) for interface in (*self.mesh_interfaces, 'ffgw') for source in self.mesh_sources for protocol in ('udp', 'tcp'))

    def close(self):
        if os.environ.get('NODE_ROLE', 'node') == 'gateway':
            from gateway_control import withdraw_gateway
            withdraw_gateway()
        # Install first, before removing old permits. The insertion is fail-closed
        # even if subsequent cleanup fails. Unrelated forwarding is unchanged.
        fd, policy = tempfile.mkstemp(prefix='ffdd-exit-close-')
        try:
            with os.fdopen(fd, 'w') as handle:
                handle.write('*filter\n-F ' + INPUT + '\n-F ' + OUTPUT + '\n-F ' + FORWARD + '\n-A ' + FORWARD + ' -i ffgw -j DROP\n-A ' + FORWARD + ' -i wgx+ -j DROP\n-A ' + FORWARD + ' -o wgx+ -j DROP\n' + ''.join('-A ' + OUTPUT + ' -s ' + source + ' -j DROP\n' for source in sorted(self.sources)) + self._gateway_dns_permissions() + '-A ' + INPUT + ' -d ' + self.node_ip + ' -p 4 -j DROP\n' + ''.join('-A ' + INPUT + ' -d ' + self.node_ip + ' -p ' + protocol + ' --dport 53 -j DROP\n' for protocol in ('udp', 'tcp')) + '-A ' + INPUT + ' -i wgx+ -j DROP\nCOMMIT\n')
            self.run('iptables-restore', '--wait', '5', '--noflush', policy)
        finally:
            remove_temporary(policy, 'ffdd-exit-')
        self.run('iptables', '-w', '-t', 'nat', '-F', NAT)
        self.status = {'phase': 'disabled', 'exits': []}

    def recover_owned(self):
        if self.discovered:
            return
        links = json.loads(self.run('ip', '-j', 'link', 'show'))
        addresses = {link['ifname']: link.get('addr_info', []) for link in json.loads(self.run('ip', '-j', 'address', 'show'))}
        owned = [link for link in links if (link.get('ifname') == 'ffgw' or link.get('ifname', '').startswith('wgx')) and link.get('ifalias') == PREFIX + link.get('ifname', '')]
        for link in owned:
            self.links.add(link['ifname'])
            self.sources.update(a['local'] for a in addresses.get(link['ifname'], []) if a.get('family') == 'inet')
        if not owned:
            self.discovered = True
            return
        # Protocol 192 is our explicit route/rule ownership tag. Never infer
        # ownership merely from reserved priorities or table numbers.
        for rule in json.loads(self.run('ip', '-j', 'rule', 'show')):
            if str(rule.get('protocol')) not in ('192', 'eigrp') or not 100 <= int(rule.get('priority', 0)) <= 163 or not 52000 <= int(rule.get('table', 0)) <= 52063:
                continue
            args = ['priority', str(rule['priority']), 'lookup', str(rule['table']), 'protocol', '192']
            if rule.get('src', 'all') != 'all':
                args += ['from', rule['src']]
            if rule.get('iif'):
                args += ['iif', rule['iif']]
            self.rules.append(tuple(args))
        for route in json.loads(self.run('ip', '-j', 'route', 'show', 'table', 'all')):
            table = route.get('table', 0)
            if str(table).isdigit() and 52000 <= int(table) <= 52063 and str(route.get('protocol')) in ('192', 'eigrp'):
                self.tables.add(int(table))
        self.discovered = True

    def cleanup(self):
        self.recover_owned()
        self.close()
        self.forward_plan.clear()
        self.input_plan.clear()
        for args in self.rules:
            self.run('ip', 'rule', 'del', *args)
        self.rules.clear()
        for table in self.tables:
            self.run('ip', 'route', 'flush', 'table', str(table), 'proto', '192')
        self.tables.clear()
        for link in sorted(self.links):
            self.run('ip', 'link', 'del', 'dev', link)
        self.links.clear()

    def _new_link(self, name, kind, *args):
        existing = json.loads(self.run('ip', '-j', 'link', 'show'))
        if any(link['ifname'] == name for link in existing):
            raise RuntimeErrorSafe('exit interface already exists; refusing adoption')
        self.run('ip', 'link', 'add', name, 'type', kind, *args)
        self.links.add(name)
        self.run('ip', 'link', 'set', 'dev', name, 'alias', PREFIX + name)

    def _rule(self, *args):
        args = (*args, 'protocol', '192')
        self.run('ip', 'rule', 'add', *args)
        self.rules.append(args)

    def apply(self, config: ExitConfig):
        self.enabled = True
        self.cleanup()
        if not config.exits:
            return self.status
        try:
            return self._apply(config)
        except Exception:
            self.close()
            self.status = {'phase': 'error', 'exits': []}
            raise RuntimeErrorSafe('exit configuration could not be applied') from None

    def _apply(self, config):
        local = {entry['local'] for link in json.loads(self.run('ip', '-j', 'address', 'show')) for entry in link.get('addr_info', []) if entry.get('family') == 'inet'}
        tunnel_addresses = {str(ipaddress.ip_interface(a).ip) for exit in config.exits for a in exit.addresses}
        dns_addresses = {dns for exit in config.exits for dns in exit.dns}
        if tunnel_addresses & (local | {self.node_ip}) or dns_addresses & (local | tunnel_addresses | {self.node_ip}):
            raise RuntimeErrorSafe('exit address conflicts with a local address')
        rules = json.loads(self.run('ip', '-j', 'rule', 'show'))
        if any(100 <= int(r.get('priority', 0)) <= 163 for r in rules):
            raise RuntimeErrorSafe('reserved exit rule range is occupied')
        routes = json.loads(self.run('ip', '-j', 'route', 'show', 'table', 'all'))
        requested_tables = {str(52000 + i) for i in range(len(config.exits))}
        if any(str(route.get('table', 'main')) in requested_tables for route in routes):
            raise RuntimeErrorSafe('reserved exit table is occupied')
        self._new_link('ffgw', 'ipip', 'local', self.node_ip, 'remote', 'any')
        self.run('ip', 'link', 'set', 'dev', 'ffgw', 'mtu', '1280', 'up')
        active = config.exits[0]
        profiles = []
        for i, exit in enumerate(config.exits):
            table, iface = 52000 + i, interface_name(exit.name)
            self._new_link(iface, 'wireguard')
            # Resolve via the ordinary host resolver before policy source rules exist.
            host, port = exit.endpoint.rsplit(':', 1)
            endpoint_ip = socket.getaddrinfo(host, int(port), socket.AF_INET, socket.SOCK_DGRAM)[0][4][0]
            fd, keyfile = tempfile.mkstemp(prefix='ffdd-wg-')
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, 'w') as handle:
                    handle.write(exit.private_key + '\n')
                self.run('wg', 'set', iface, 'private-key', keyfile, 'peer', exit.public_key, 'endpoint', endpoint_ip + ':' + port, 'allowed-ips', '0.0.0.0/0', 'persistent-keepalive', '25')
            finally:
                remove_temporary(keyfile, 'ffdd-wg-')
            for address in exit.addresses:
                self.sources.add(str(ipaddress.ip_interface(address).ip))
                self.run('ip', 'address', 'add', address, 'dev', iface, 'noprefixroute')
            self.run('ip', 'link', 'set', 'dev', iface, 'mtu', '1280', 'up')
            self.tables.add(table)
            self.run('ip', 'route', 'add', 'unreachable', 'default', 'metric', '1000', 'table', str(table), 'proto', '192')
            self.run('ip', 'route', 'add', 'default', 'dev', iface, 'metric', '10', 'table', str(table), 'proto', '192')
            self.run('ip', 'route', 'add', '10.200.0.0/16', 'dev', 'ffgw', 'table', str(table), 'proto', '192')
            self._rule('priority', str(100 + i), 'iif', iface, 'lookup', str(table))
            for address in exit.addresses:
                self._rule('priority', str(100 + i), 'from', str(ipaddress.ip_interface(address).ip) + '/32', 'lookup', str(table))
            profiles.append({'name': exit.name, 'comment': exit.comment, 'interface': iface, 'endpoint': exit.endpoint, 'dns': list(exit.dns), 'active': exit == active, 'state': 'ready'})
        iface = interface_name(active.name)
        self._rule('priority', str(100 + config.exits.index(active)), 'iif', 'ffgw', 'lookup', str(52000 + config.exits.index(active)))
        # Build behind the existing ffgw DROP; remove it only as the last step.
        self.fw(FORWARD, '-i', 'ffgw', '!', '-s', '10.200.0.0/16', '-j', 'DROP')
        for dns in active.dns:
            for protocol in ('udp', 'tcp'):
                self.fw(FORWARD, '-i', 'ffgw', '-s', '10.200.0.0/16', '-o', iface, '-d', dns, '-p', protocol, '--dport', '53', '-j', 'ACCEPT')
        for network in DENIED:
            self.fw(FORWARD, '-i', 'ffgw', '-d', network, '-j', 'DROP')
        self.fw(FORWARD, '-i', 'ffgw', '-s', '10.200.0.0/16', '-o', iface, '-j', 'ACCEPT')
        for dns in active.dns:
            for protocol in ('udp', 'tcp'):
                self.fw(FORWARD, '-i', iface, '-o', 'ffgw', '-s', dns, '-d', '10.200.0.0/16', '-p', protocol, '--sport', '53', '-m', 'conntrack', '--ctstate', 'ESTABLISHED', '-j', 'ACCEPT')
        for network in DENIED:
            self.fw(FORWARD, '-i', iface, '-s', network, '-j', 'DROP')
        self.fw(FORWARD, '-i', iface, '-o', 'ffgw', '-d', '10.200.0.0/16', '-m', 'conntrack', '--ctstate', 'ESTABLISHED,RELATED', '-j', 'ACCEPT')
        self.fw(FORWARD, '-i', 'ffgw', '-j', 'DROP')
        self.fw(FORWARD, '-i', 'wgx+', '-j', 'DROP')
        self.fw(FORWARD, '-o', 'wgx+', '-j', 'DROP')
        self.fw(NAT, '-s', '10.200.0.0/16', '-o', iface, '-j', 'MASQUERADE', table='nat')
        for source in sorted(self.sources):
            self.fw(INPUT, '-i', 'wgx+', '-d', source, '-m', 'conntrack', '--ctstate', 'ESTABLISHED,RELATED', '-j', 'ACCEPT')
        self.fw(INPUT, '-i', 'wgx+', '-j', 'DROP')
        for source in self.mesh_sources:
            for mesh_if in self.mesh_interfaces:
                self.fw(INPUT, '-i', mesh_if, '-s', source, '-d', self.node_ip, '-p', '4', '-j', 'ACCEPT')
            for mesh_if in (*self.mesh_interfaces, 'ffgw'):
                for protocol in ('udp', 'tcp'):
                    self.fw(INPUT, '-i', mesh_if, '-s', source, '-d', self.node_ip, '-p', protocol, '--dport', '53', '-j', 'ACCEPT')
        self.fw(INPUT, '-d', self.node_ip, '-p', '4', '-j', 'DROP')
        for protocol in ('udp', 'tcp'):
            self.fw(INPUT, '-d', self.node_ip, '-p', protocol, '--dport', '53', '-j', 'DROP')
        fd, policy = tempfile.mkstemp(prefix='ffdd-exit-input-')
        try:
            with os.fdopen(fd, 'w') as handle:
                handle.write('*filter\n-F ' + INPUT + '\n' + ''.join('-A ' + INPUT + ' ' + ' '.join(args) + '\n' for args in self.input_plan) + 'COMMIT\n')
            self.run('iptables-restore', '--wait', '5', '--noflush', policy)
        finally:
            remove_temporary(policy, 'ffdd-exit-')
        output_rules = []
        for exit in config.exits:
            for address in exit.addresses:
                source = str(ipaddress.ip_interface(address).ip)
                output_rules += [('-s', source, '-o', interface_name(exit.name), '-j', 'ACCEPT'), ('-s', source, '-j', 'DROP')]
        current_sources = {str(ipaddress.ip_interface(address).ip) for exit in config.exits for address in exit.addresses}
        output_rules += [('-s', source, '-j', 'DROP') for source in sorted(self.sources - current_sources)]
        fd, policy = tempfile.mkstemp(prefix='ffdd-exit-output-')
        try:
            with os.fdopen(fd, 'w') as handle:
                handle.write('*filter\n-F ' + OUTPUT + '\n' + ''.join('-A ' + OUTPUT + ' ' + ' '.join(args) + '\n' for args in output_rules) + 'COMMIT\n')
            self.run('iptables-restore', '--wait', '5', '--noflush', policy)
        finally:
            remove_temporary(policy, 'ffdd-exit-')
        self.active_source = str(ipaddress.ip_interface(active.addresses[0]).ip)
        self.active_table = 52000
        self.status = {'phase': 'ready', 'exits': profiles, 'active': active.name, 'dns': list(active.dns), 'ifname': iface}
        self.set_health(False)
        return self.status

    def set_health(self, reachable):
        if not reachable and os.environ.get('NODE_ROLE', 'node') == 'gateway':
            from gateway_control import withdraw_gateway
            withdraw_gateway()
        guards = [] if reachable else [('-i', 'ffgw', '-j', 'DROP'), ('-i', 'wgx+', '-j', 'DROP'), ('-o', 'wgx+', '-j', 'DROP')]
        fd, policy = tempfile.mkstemp(prefix='ffdd-exit-health-')
        try:
            with os.fdopen(fd, 'w') as handle:
                handle.write('*filter\n-F ' + FORWARD + '\n' + ''.join('-A ' + FORWARD + ' ' + ' '.join(args) + '\n' for args in guards + self.forward_plan) + 'COMMIT\n')
            self.run('iptables-restore', '--wait', '5', '--noflush', policy)
        finally:
            remove_temporary(policy, 'ffdd-exit-')
        self.status['phase'] = 'ready' if reachable else 'unreachable'
        self.status['reachable'] = reachable
        for profile in self.status['exits']:
            if profile['active']:
                profile['state'] = self.status['phase']
                profile['reachable'] = reachable

    def probe(self, target='1.1.1.1'):
        """Bound, source-policy-routed end-to-end ICMP; never announces a gateway."""
        if self.status['phase'] not in ('ready', 'unreachable'):
            return False
        try:
            # Linux removes device routes when a WG link goes down. Reinstall
            # only our active-table route; the unreachable fallback stays intact.
            self.run('ip', 'route', 'replace', 'default', 'dev', self.status['ifname'], 'metric', '10', 'table', str(self.active_table), 'proto', '192')
            # Device-bound ping can bypass source policy and falsely report health
            # after that route disappeared. Binding the actual WG address tests
            # the same source rule used by the separate DNS daemon.
            self.run('ping', '-n', '-I', self.active_source, '-c', '1', '-W', '3', str(ipaddress.IPv4Address(target)))
        except Exception:
            self.set_health(False)
            return False
        self.set_health(True)
        return True

    def write_status(self, path):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=target.parent, prefix='.exits-')
        try:
            with os.fdopen(fd, 'w') as handle:
                public = dict(self.status, timestamp=int(time.time()), enabled=self.enabled, active=self.status.get('active'))
                json.dump(public, handle)
            os.chmod(temporary, 0o644)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                remove_temporary(temporary, '.exits-', target.parent)
