import type { NodeRow } from './types';

export type GatewaySelection = { preferred?: string; selected?: string };

export const GATEWAY_COLUMNS: Array<{ key: keyof NodeRow; label: string }> = [
  { key: 'node', label: 'Node' },
  { key: 'ip', label: 'IP' },
  { key: 'best_next_hop', label: 'Best Next Hop' },
  { key: 'speed', label: 'Speed' },
  { key: 'usage', label: 'Usage' },
  { key: 'preferred', label: 'Bevorzugt' },
  { key: 'selected', label: 'Aktiv' },
  { key: 'brc', label: 'BRC' },
];

/** Preference and actual selection are independent; an unavailable preference
 * must not make a fallback gateway appear preferred. Aggregate IPs are the
 * authoritative bmxd state; per-row flags support older/partial payloads. */
export function gatewayIndicators(row: NodeRow, selection: GatewaySelection) {
  const matches = (ip: string | undefined, flag: boolean | undefined): boolean => {
    if (ip !== undefined) {
      return Boolean(row.ip && ip !== '0.0.0.0' && ip !== '-' && row.ip === ip);
    }
    return flag === true;
  };
  return {
    preferred: matches(selection.preferred, row.preferred),
    active: matches(selection.selected, row.selected),
  };
}
