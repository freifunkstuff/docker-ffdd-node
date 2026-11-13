"""Strict runtime health contracts shared by announcement and admission."""
import math
import time

MAX_STATUS_AGE = 15
MAX_REPAIR_SECONDS = 10


def valid_timestamp(value, now=None):
    """Accept only finite, nonnegative numeric timestamps not in the future."""
    now = time.time() if now is None else now
    try:
        return (type(value) in (int, float) and math.isfinite(value)
                and type(now) in (int, float) and math.isfinite(now)
                and 0 <= value <= now)
    except (OverflowError, ValueError, TypeError):
        return False


def fresh_ready(record, now=None):
    now = time.time() if now is None else now
    return (isinstance(record, dict) and record.get('state') == 'ready'
            and valid_timestamp(record.get('timestamp'), now)
            and now - record['timestamp'] <= MAX_STATUS_AGE)


def peer_confirmed(record, node, key, now=None):
    """Confirm exact peer identity using only the authority for this state."""
    if fresh_ready(record, now):
        peers = record.get('peers')
    elif (isinstance(record, dict) and record.get('state') == 'repairing'
          and runtime_ready(record, now)):
        peers = record.get('confirmed_peers')
    else:
        return False
    if not isinstance(peers, dict) or not isinstance(key, str):
        return False
    entry = peers.get(str(node))
    return (isinstance(entry, dict) and entry.get('key') == key
            and entry.get('applied') is True)


def runtime_ready(record, now=None):
    """Service readiness includes only an explicit, bounded maintenance lease."""
    now = time.time() if now is None else now
    if fresh_ready(record, now):
        return True
    if not isinstance(record, dict) or record.get('state') != 'repairing':
        return False
    stamp, success, start = (record.get(name) for name in
                             ('timestamp', 'last_success_at', 'repair_started_at'))
    if not all(valid_timestamp(value, now) for value in (stamp, success, start)):
        return False
    return (success <= start <= stamp
            and now < min(success + MAX_STATUS_AGE, start + MAX_REPAIR_SECONDS))
