export const RESEARCH_AGENT = 'IvySurfResearchBot/1.0 (https://ivysurf.com/legal/contact/)';

export async function boundedText(response: Response, limit = 1_000_000): Promise<string> {
  if (!response.body) throw new Error('empty_response');
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > limit) throw new Error('response_too_large');
      chunks.push(value);
    }
  } finally {
    await reader.cancel();
  }
  return Buffer.concat(chunks).toString('utf8');
}

export function researchFetch(url: URL, fetcher: typeof fetch = fetch) {
  return fetcher(url, {
    headers: { 'User-Agent': RESEARCH_AGENT },
    redirect: 'error',
    signal: AbortSignal.timeout(20_000),
  });
}

// Conservative discovery only: an applicable disallow wins, even over an Allow exception.
// A requested crawl delay requires operator review instead of an automatic retry.
export function permitsResearch(robots: string, path: string): boolean {
  let agents: string[] = [];
  let hasRules = false;
  for (const raw of robots.split(/\r?\n/)) {
    const line = raw.split('#')[0]!.trim();
    const colon = line.indexOf(':');
    if (colon < 0) continue;
    const key = line.slice(0, colon).trim().toLowerCase();
    const value = line.slice(colon + 1).trim();
    if (key === 'user-agent') {
      if (hasRules) agents = [];
      hasRules = false;
      agents.push(value.toLowerCase());
      continue;
    }
    hasRules = true;
    if (!agents.some((agent) => agent === '*' || 'ivysurfresearchbot'.includes(agent))) continue;
    if (key === 'crawl-delay' && value !== '0') return false;
    if (key === 'disallow' && value) {
      const anchored = value.endsWith('$');
      const pattern = (anchored ? value.slice(0, -1) : value)
        .split('*')
        .map((part) => part.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
        .join('.*');
      if (new RegExp(`^${pattern}${anchored ? '$' : ''}`).test(path)) return false;
    }
  }
  return true;
}
