#!/usr/bin/env python3
"""Gateway-only PID1 shutdown ordering: withdraw before exit/backbone teardown."""
import signal
import subprocess
import time


def shutdown_services(runner=subprocess.run):
    for name in ('gateway-control', 'exits', 'statistics', 'gateway-runtime', 'gateway-api', 'fastd', 'bmxd'):
        try:
            runner(['sv', '-w', '2', 'down', '/etc/service/' + name], capture_output=True, timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            pass


def main():
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    child = subprocess.Popen(['runsvdir', '-P', '/etc/service'])
    try:
        while not stopping and child.poll() is None:
            time.sleep(.1)
    finally:
        shutdown_services()
        child.terminate()
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
    return 0 if stopping else (child.returncode or 0)


if __name__ == '__main__':
    raise SystemExit(main())
