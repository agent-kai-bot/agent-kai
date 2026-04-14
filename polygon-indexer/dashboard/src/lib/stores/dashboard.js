import { browser } from '$app/environment';
import { writable } from 'svelte/store';

const OVERVIEW_URL = '/api/v1/polygon/overview';
const WHALES_URL = '/api/v1/polygon/whale-transfers?limit=30';
const STREAM_URL = '/api/v1/polygon/stream';

const initialState = {
  loading: false,
  error: null,
  connected: false,
  lastEventAt: null,
  headTick: 0,
  overview: null,
  whales: [],
  inspector: {
    open: false,
    address: null,
    loading: false,
    error: null,
    token: null,
    holders: [],
    transfers: []
  }
};

function sortTokens(tokens = []) {
  return [...tokens].sort((left, right) => (right.transfers_24h || 0) - (left.transfers_24h || 0));
}

function trimBlocks(blocks = []) {
  return [...blocks]
    .sort((left, right) => (right.block_number || 0) - (left.block_number || 0))
    .slice(0, 40);
}

function trimWhales(rows = []) {
  return [...rows].slice(0, 30);
}

function parsePayload(raw) {
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

function buildSinceWindow(days = 14) {
  return new Date(Date.now() - days * 24 * 60 * 60 * 1000).toISOString();
}

function whaleKey(row) {
  return [row?.tx_hash, row?.contract_address, row?.value, row?.timestamp].join(':');
}

function mergeOverview(state, data) {
  const previous = state.overview || {};
  return {
    ...state,
    loading: false,
    error: null,
    overview: {
      ...previous,
      ...data,
      tokens: sortTokens(data.tokens || previous.tokens || []),
      recent_blocks: trimBlocks(data.recent_blocks || previous.recent_blocks || [])
    }
  };
}

async function requestJson(path, fetchImpl = fetch) {
  const response = await fetchImpl(path);
  if (!response.ok) {
    throw new Error(`Request failed: ${response.status}`);
  }
  return response.json();
}

function createDashboardStore() {
  const { subscribe, update, set } = writable(initialState);
  let eventSource = null;
  let overviewTimer = null;
  let bootstrapped = false;

  function setError(error) {
    update((state) => ({
      ...state,
      loading: false,
      error: error instanceof Error ? error.message : 'Unknown error'
    }));
  }

  async function refreshOverview(fetchImpl = fetch) {
    const response = await requestJson(OVERVIEW_URL, fetchImpl);
    update((state) => mergeOverview(state, response.data || {}));
  }

  async function refreshWhales(fetchImpl = fetch) {
    const response = await requestJson(WHALES_URL, fetchImpl);
    update((state) => ({
      ...state,
      whales: trimWhales(response.data || [])
    }));
  }

  function startPolling(fetchImpl = fetch) {
    if (!browser) {
      return;
    }
    stopPolling();
    overviewTimer = window.setInterval(() => {
      void refreshOverview(fetchImpl).catch(setError);
    }, 30000);
  }

  function stopPolling() {
    if (overviewTimer) {
      window.clearInterval(overviewTimer);
      overviewTimer = null;
    }
  }

  function connectStream() {
    if (!browser || eventSource) {
      return;
    }
    eventSource = new EventSource(STREAM_URL);
    eventSource.addEventListener('open', () => {
      update((state) => ({ ...state, connected: true }));
    });
    eventSource.addEventListener('error', () => {
      update((state) => ({ ...state, connected: false }));
    });
    eventSource.addEventListener('head', (event) => {
      const payload = parsePayload(event.data);
      if (!payload) {
        return;
      }
      update((state) => {
        const overview = state.overview || {};
        const existingBlocks = overview.recent_blocks || [];
        const nextBlocks = trimBlocks([payload, ...existingBlocks.filter((row) => row.block_number !== payload.block_number)]);
        return {
          ...state,
          connected: true,
          lastEventAt: Date.now(),
          headTick: state.headTick + 1,
          overview: {
            ...overview,
            chain_head: payload.chain_head ?? overview.chain_head,
            last_indexed_block: payload.last_indexed ?? payload.block_number ?? overview.last_indexed_block,
            head_lag_blocks: payload.head_lag_blocks ?? overview.head_lag_blocks,
            backfill_complete: payload.backfill_complete ?? overview.backfill_complete,
            backfill_pct: payload.backfill_pct ?? overview.backfill_pct,
            gas_current_gwei: payload.gas_current_gwei ?? overview.gas_current_gwei,
            gas_avg_20_block_gwei: payload.gas_avg_20_block_gwei ?? overview.gas_avg_20_block_gwei,
            tps_current: payload.tps_current ?? overview.tps_current,
            recent_blocks: nextBlocks
          }
        };
      });
    });
    eventSource.addEventListener('whale', (event) => {
      const payload = parsePayload(event.data);
      if (!payload) {
        return;
      }
      const incomingKey = whaleKey(payload);
      update((state) => ({
        ...state,
        connected: true,
        lastEventAt: Date.now(),
        whales: trimWhales([payload, ...state.whales.filter((row) => whaleKey(row) !== incomingKey)])
      }));
    });
    eventSource.addEventListener('status', (event) => {
      const payload = parsePayload(event.data);
      if (!payload) {
        return;
      }
      update((state) => {
        const overview = state.overview || {};
        return {
          ...state,
          connected: true,
          lastEventAt: Date.now(),
          overview: {
            ...overview,
            chain_head: payload.chain_head ?? overview.chain_head,
            last_indexed_block: payload.last_indexed ?? overview.last_indexed_block,
            head_lag_blocks: payload.lag ?? overview.head_lag_blocks,
            backfill_complete: payload.backfill_complete ?? overview.backfill_complete,
            backfill_pct: payload.backfill_pct ?? overview.backfill_pct,
            total_transfers_indexed: payload.total_transfers_indexed ?? overview.total_transfers_indexed,
            last_updated_at: payload.last_updated_at ?? overview.last_updated_at
          }
        };
      });
    });
  }

  async function bootstrap(fetchImpl = fetch) {
    if (!browser) {
      return;
    }
    if (bootstrapped) {
      return;
    }
    bootstrapped = true;
    update((state) => ({ ...state, loading: true, error: null }));
    try {
      const [overviewResponse, whalesResponse] = await Promise.all([
        requestJson(OVERVIEW_URL, fetchImpl),
        requestJson(WHALES_URL, fetchImpl)
      ]);
      update((state) => ({
        ...mergeOverview(state, overviewResponse.data || {}),
        whales: trimWhales(whalesResponse.data || [])
      }));
      connectStream();
      startPolling(fetchImpl);
    } catch (error) {
      setError(error);
    }
  }

  async function openToken(address, fetchImpl = fetch) {
    const contractAddress = address?.toLowerCase() || '';
    update((state) => ({
      ...state,
      inspector: {
        ...state.inspector,
        open: true,
        address: contractAddress,
        loading: true,
        error: null
      }
    }));
    try {
      const encoded = encodeURIComponent(contractAddress);
      const since = encodeURIComponent(buildSinceWindow());
      const [tokenResponse, holdersResponse, transfersResponse] = await Promise.all([
        requestJson(`/api/v1/polygon/tokens/${encoded}`, fetchImpl),
        requestJson(`/api/v1/polygon/tokens/${encoded}/holders?limit=10`, fetchImpl),
        requestJson(`/api/v1/polygon/tokens/${encoded}/transfers?limit=20&since=${since}`, fetchImpl)
      ]);
      update((state) => ({
        ...state,
        inspector: {
          open: true,
          address: contractAddress,
          loading: false,
          error: null,
          token: tokenResponse.data || null,
          holders: holdersResponse.data?.holders || [],
          transfers: transfersResponse.data || []
        }
      }));
    } catch (error) {
      update((state) => ({
        ...state,
        inspector: {
          ...state.inspector,
          open: true,
          address: contractAddress,
          loading: false,
          error: error instanceof Error ? error.message : 'Unable to load token detail'
        }
      }));
    }
  }

  function closeToken() {
    update((state) => ({
      ...state,
      inspector: {
        ...state.inspector,
        open: false
      }
    }));
  }

  function destroy() {
    stopPolling();
    if (eventSource) {
      eventSource.close();
      eventSource = null;
    }
    bootstrapped = false;
    set(initialState);
  }

  return {
    subscribe,
    bootstrap,
    refreshOverview,
    refreshWhales,
    openToken,
    closeToken,
    destroy
  };
}

export const dashboard = createDashboardStore();
