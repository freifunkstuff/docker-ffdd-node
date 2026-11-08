import type { StatisticsHistoryPayload, SysinfoPayload } from './types';

export interface StatisticsLivePayload {
  version: 1;
  epoch?: string;
  updated_at: string | number;
  samples: SysinfoPayload[];
}

/** Merge a small live tail, never joining samples across collector restarts. */
export function mergeLiveHistory(history: StatisticsHistoryPayload | null, live: StatisticsLivePayload | null): StatisticsHistoryPayload | null {
  if (!live || live.version !== 1 || !Array.isArray(live.samples) || !(Number(live.updated_at) > 0)) return history;
  if (history && Number(history.updated_at) >= Number(live.updated_at)) return history;
  const sameEpoch = !!history && history.epoch === live.epoch;
  const base: StatisticsHistoryPayload = sameEpoch ? history! : {
    version: 1, history_seconds: 3600, detail_seconds: 300,
    epoch: live.epoch, updated_at: live.updated_at,
    resolutions: { '1': [], '15': [] },
  };
  return {
    ...base, updated_at: live.updated_at,
    resolutions: { ...base.resolutions, '1': [...base.resolutions['1'], ...live.samples] },
  };
}
