export function connectionText(value: boolean | undefined): string {
  return value === true ? 'Verbunden' : value === false ? 'Getrennt' : 'Unbekannt';
}

export function meshStatusText(connected: boolean | undefined, stable: boolean | undefined): string {
  return stable ? 'Verbunden' : connected ? 'Stabilisiert sich' : connectionText(connected);
}

export function backboneStatusText(value: unknown): string {
  switch (typeof value === 'string' ? value.toLowerCase() : '') {
    case 'connected': return 'Verbunden';
    case 'disconnected': return 'Getrennt';
    case 'connecting': return 'Verbindungsaufbau';
    case 'stabilizing': return 'Stabilisiert sich';
    case 'waiting': return 'Wartet';
    case 'disabled': return 'Deaktiviert';
    case 'stale': return 'Veraltet';
    case 'error': return 'Fehler';
    default: return 'Unbekannt';
  }
}
