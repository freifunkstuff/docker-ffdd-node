"""Runit entry point. Configuration reload failures close the exit immediately."""
from __future__ import annotations
import os
import signal
import time
from pathlib import Path
from exit_config import load_config
from exit_runtime import Controller
from exit_observation import read_node_primary


def main():
    running = True
    def stop(*_):
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    controller = None
    last = object()
    status_path = os.environ.get('EXIT_STATUS_FILE', '/run/freifunk/www/exits.json')
    try:
        while running:
            path = os.environ.get('EXIT_CONFIG_FILE', '')
            if not path:
                if controller is not None:
                    controller.cleanup()
                    controller = None
                Controller('0.0.0.0').write_status(status_path)
                time.sleep(5)
                continue
            node_ip = read_node_primary()
            if not node_ip:
                if controller is not None:
                    controller.close()
                    controller.status = {'phase': 'configuring', 'exits': []}
                    controller.write_status(status_path)
                    last = object()
                time.sleep(1)
                continue
            if controller is not None and controller.node_ip != node_ip:
                controller.cleanup()
                controller = None
                last = object()
            if controller is None:
                controller = Controller(node_ip, filter(None, os.environ.get('EXIT_MESH_SOURCE_NETS', '10.200.0.0/15').split(',')), mesh_interfaces=os.environ.get('MESH_INTERFACES', '').replace(',', ' ').split())
            controller.enabled = True
            try:
                fingerprint = (path, Path(path).stat().st_mtime_ns, Path(path).stat().st_size) if path else None
                if fingerprint != last:
                    # Close BEFORE parsing: malformed replacements cannot leave a
                    # previously working secret configuration in use.
                    controller.cleanup()
                    if path:
                        if Path(path).stat().st_mode & 0o077:
                            print('exit configuration permissions permit other readers', flush=True)
                        controller.apply(load_config(path))
                    last = fingerprint
                if path:
                    controller.probe()
            except Exception:
                controller.close()
                controller.status = {'phase': 'error', 'exits': []}
                last = object()
                print('exit service: configuration or network operation failed', flush=True)
            controller.write_status(status_path)
            time.sleep(5)
    finally:
        if controller:
            controller.cleanup()
            controller.write_status(status_path)

if __name__ == '__main__':
    main()
