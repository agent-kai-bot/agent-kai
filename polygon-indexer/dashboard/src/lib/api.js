export async function fetchEnvelope(path) {
  const response = await fetch(path, {
    headers: {
      accept: 'application/json'
    },
    cache: 'no-store'
  });

  if (!response.ok) {
    throw new Error(`Request failed: ${response.status}`);
  }

  const body = await response.json();
  if (!body?.ok) {
    throw new Error('API returned a non-ok envelope');
  }

  return body.data;
}

export function polygonscanTxUrl(txHash) {
  return `https://polygonscan.com/tx/${txHash}`;
}
