import type { NodeRow } from './types';

/** Nodes search deliberately covers only the node ID and that row's own IP. */
export function matchesNodeFilter(row: NodeRow, query: string): boolean {
  const needle = query.trim().toLowerCase();
  if (!needle) return true;
  return [row.node, row.ip].some(value => value !== null && value !== undefined
    && String(value).toLowerCase().includes(needle));
}
