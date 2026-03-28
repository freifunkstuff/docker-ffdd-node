export type SysinfoPayload = {
  timestamp?: string;
  data?: {
    common?: {
      node?: string;
      community?: string;
      domain?: string;
      ip?: string;
    };
    system?: {
      uptime?: string;
      uptime_string?: string;
      model?: string;
      board?: string;
      cpucount?: string;
    };
    contact?: {
      name?: string;
      email?: string;
      location?: string;
      note?: string;
    };
    statistic?: {
      cpu_load?: string;
      meminfo_MemTotal?: string;
      meminfo_MemFree?: string;
      interfaces?: {
        tbb_fastd_rx?: string;
        tbb_fastd_tx?: string;
      };
    };
  };
};

export type NodeRow = {
  node?: string;
  ip?: string;
  interface?: string;
  rtq?: string;
  rq?: string;
  tq?: string;
  best_next_hop?: string;
  brc?: string;
  speed?: string;
  usage?: string;
  type?: string;
};

export type NodesPayload = {
  timestamp?: string;
  bmxd?: {
    links?: NodeRow[];
    originators?: NodeRow[];
    gateways?: {
      selected?: string;
      preferred?: string;
      gateways?: NodeRow[];
    };
  };
};

export type BackbonePeer = {
  type?: string;
  host?: string;
  port?: string;
  interface?: string;
  status?: string;
};

export type BackbonePayload = {
  timestamp?: string;
  peers?: BackbonePeer[];
};
