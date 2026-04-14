const compactNumber = new Intl.NumberFormat('en-US', {
  notation: 'compact',
  maximumFractionDigits: 1
});

const integerNumber = new Intl.NumberFormat('en-US', {
  maximumFractionDigits: 0
});

const oneDecimalNumber = new Intl.NumberFormat('en-US', {
  minimumFractionDigits: 0,
  maximumFractionDigits: 1
});

export function formatCompactNumber(value) {
  return compactNumber.format(Number(value || 0));
}

export function formatInteger(value) {
  return integerNumber.format(Number(value || 0));
}

export function formatNumeric(value, decimals = 1) {
  const numeric = Number(value || 0);
  return new Intl.NumberFormat('en-US', {
    minimumFractionDigits: 0,
    maximumFractionDigits: decimals
  }).format(numeric);
}

export function formatPercent(value, decimals = 1) {
  return `${formatNumeric(value, decimals)}%`;
}

export function formatAddress(address) {
  if (!address) {
    return 'unknown';
  }
  if (address.length <= 12) {
    return address;
  }
  return `${address.slice(0, 6)}..${address.slice(-4)}`;
}

export function formatTimestamp(value) {
  if (!value) {
    return 'No update';
  }
  const date = new Date(value);
  return date.toLocaleString('en-US', {
    hour: 'numeric',
    minute: '2-digit',
    second: '2-digit',
    month: 'short',
    day: 'numeric'
  });
}

export function formatTimeAgo(value) {
  if (!value) {
    return 'now';
  }
  const deltaMs = Date.now() - new Date(value).getTime();
  const deltaSeconds = Math.max(0, Math.floor(deltaMs / 1000));
  if (deltaSeconds < 60) {
    return `${deltaSeconds}s ago`;
  }
  if (deltaSeconds < 3600) {
    return `${Math.floor(deltaSeconds / 60)}m ago`;
  }
  if (deltaSeconds < 86400) {
    return `${Math.floor(deltaSeconds / 3600)}h ago`;
  }
  return `${Math.floor(deltaSeconds / 86400)}d ago`;
}

export function formatTokenAmount(value, decimals = 2) {
  const numeric = Number(value || 0);
  if (!Number.isFinite(numeric)) {
    return String(value ?? '0');
  }
  if (Math.abs(numeric) >= 1000) {
    return compactNumber.format(numeric);
  }
  return new Intl.NumberFormat('en-US', {
    minimumFractionDigits: 0,
    maximumFractionDigits: decimals
  }).format(numeric);
}

export function formatUsd(value) {
  if (value === null || value === undefined) {
    return null;
  }
  const numeric = Number(value);
  if (numeric >= 1000000) {
    return `$${(numeric / 1000000).toFixed(2)}M`;
  }
  if (numeric >= 1000) {
    return `$${(numeric / 1000).toFixed(1)}K`;
  }
  return `$${formatNumeric(numeric, 0)}`;
}
