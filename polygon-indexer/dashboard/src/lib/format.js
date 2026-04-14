const compactNumber = new Intl.NumberFormat('en-US', {
  notation: 'compact',
  maximumFractionDigits: 1
});

const fullNumber = new Intl.NumberFormat('en-US', {
  maximumFractionDigits: 1
});

const usdCompact = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  notation: 'compact',
  maximumFractionDigits: 1
});

const usdFull = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  maximumFractionDigits: 0
});

export function formatNumber(value, fallback = '0') {
  if (value === null || value === undefined || value === '') {
    return fallback;
  }
  const numeric = Number(value);
  return Number.isFinite(numeric) ? fullNumber.format(numeric) : fallback;
}

export function formatCompact(value, fallback = '0') {
  if (value === null || value === undefined || value === '') {
    return fallback;
  }
  const numeric = Number(value);
  return Number.isFinite(numeric) ? compactNumber.format(numeric) : fallback;
}

export function formatUsd(value, compact = true) {
  if (value === null || value === undefined || value === '') {
    return 'Unpriced';
  }
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return 'Unpriced';
  }
  return compact ? usdCompact.format(numeric) : usdFull.format(numeric);
}

export function formatPercent(value, digits = 1) {
  if (value === null || value === undefined || value === '') {
    return '0%';
  }
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return '0%';
  }
  return `${numeric.toFixed(digits)}%`;
}

export function formatGwei(value) {
  if (value === null || value === undefined || value === '') {
    return '0';
  }
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return '0';
  }
  return numeric.toFixed(numeric >= 100 ? 0 : 1);
}

export function formatAmount(value, digits = 1) {
  if (value === null || value === undefined || value === '') {
    return '0';
  }
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return String(value);
  }
  return numeric.toLocaleString('en-US', {
    maximumFractionDigits: digits
  });
}

export function shortAddress(value, leading = 6, trailing = 4) {
  if (!value || value.length <= leading + trailing + 2) {
    return value ?? '';
  }
  return `${value.slice(0, leading + 2)}..${value.slice(-trailing)}`;
}

export function timeAgo(value) {
  if (!value) {
    return 'now';
  }
  const timestamp = new Date(value);
  if (Number.isNaN(timestamp.getTime())) {
    return 'now';
  }
  const seconds = Math.max(0, Math.floor((Date.now() - timestamp.getTime()) / 1000));
  if (seconds < 60) {
    return `${seconds}s ago`;
  }
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) {
    return `${minutes}m ago`;
  }
  const hours = Math.floor(minutes / 60);
  if (hours < 24) {
    return `${hours}h ago`;
  }
  return `${Math.floor(hours / 24)}d ago`;
}

export function formatTimestamp(value) {
  if (!value) {
    return 'Unavailable';
  }
  const timestamp = new Date(value);
  if (Number.isNaN(timestamp.getTime())) {
    return 'Unavailable';
  }
  return timestamp.toLocaleString('en-US', {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit'
  });
}
