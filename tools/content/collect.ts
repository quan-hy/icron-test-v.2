import { mkdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { searchCommons } from '../images/search.ts';
import { boundedText, permitsResearch, researchFetch } from './research-http.ts';
import { DISCOVERY_SOURCES } from './sources.ts';

const plainText = (value: string) =>
  value
    .replace(/<[^>]*>/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();

export function officialMetadata(html: string, source: string) {
  const base = new URL(source);
  const title = plainText(html.match(/<title\b[^>]*>([\s\S]*?)<\/title>/i)?.[1] ?? '').slice(
    0,
    180,
  );
  if (!title) throw new Error('page_title_missing');
  const related = new Map<string, { title: string; url: string }>();
  for (const match of html.matchAll(/<a\b[^>]*\bhref\s*=\s*(["'])(.*?)\1[^>]*>([\s\S]*?)<\/a>/gi)) {
    const text = plainText(match[3] ?? '').slice(0, 180);
    if (!/surf|beach|coast|서핑|해변|해수욕장/i.test(text)) continue;
    try {
      const url = new URL(match[2]!.replace(/&amp;/g, '&'), base);
      url.hash = '';
      if (
        url.origin !== base.origin ||
        url.username ||
        url.password ||
        url.href === base.href ||
        /login|account|sign.?in|token|password/i.test(url.href)
      )
        continue;
      related.set(url.href, { title: text, url: url.href });
      if (related.size === 6) break;
    } catch {
      /* Ignore malformed source links. */
    }
  }
  return { title, url: base.href, related: [...related.values()] };
}

export async function collectOfficial(url: string, fetcher: typeof fetch = fetch) {
  // Only the checked-in exact sources may initiate requests; discovered links are not crawled.
  if (!DISCOVERY_SOURCES.some((source) => source.url === url))
    throw new Error('source_not_allowed');
  const target = new URL(url);
  const robots = await researchFetch(new URL('/robots.txt', target), fetcher);
  if (robots.status !== 404) {
    if (!robots.ok || !robots.headers.get('content-type')?.includes('text/plain'))
      throw new Error('robots_unverified');
    if (!permitsResearch(await boundedText(robots, 100_000), target.pathname + target.search))
      throw new Error('robots_requires_manual_review');
  } else {
    await robots.body?.cancel();
  }
  const response = await researchFetch(target, fetcher);
  if (!response.ok) throw new Error(`source_http_${response.status}`);
  if (!response.headers.get('content-type')?.includes('text/html'))
    throw new Error('source_not_html');
  return officialMetadata(await boundedText(response), url);
}

export async function collectCandidates(now = new Date(), fetcher: typeof fetch = fetch) {
  const week = Math.floor(now.getTime() / (7 * 86_400_000));
  const regions = [];
  for (const source of DISCOVERY_SOURCES) {
    const query = source.photos[week % source.photos.length]!;
    // Sequential, bounded source reads. No backoff loops, image downloads or community export.
    const official = await collectOfficial(source.url, fetcher).then(
      (data) => ({ status: 'collected' as const, data }),
      (error: unknown) => ({
        status: 'needs_attention' as const,
        error: error instanceof Error ? error.message : 'source_failed',
      }),
    );
    const photos = await searchCommons(query, fetcher).then(
      (data) => ({ status: 'collected' as const, data }),
      (error: unknown) => ({
        status: 'needs_attention' as const,
        error: error instanceof Error ? error.message : 'photos_failed',
      }),
    );
    regions.push({ region: source.region, official, photos });
  }
  return {
    schema: 1,
    collectedAt: now.toISOString(),
    reviewStatus: 'pending' as const,
    approvedForPublication: false as const,
    notice:
      'Research candidates only. Source titles and links are not verified travel advice. Photo location, attribution, license and depicted-person restrictions require human review. Never copy community posts into this artifact. No automatic publication.',
    regions,
  };
}

async function main() {
  const args = process.argv.slice(2);
  if (!args.length) {
    console.log(
      JSON.stringify(
        {
          mode: 'no-network-preview',
          schedule: 'Monday 03:17 UTC / 12:17 KST',
          sources: DISCOVERY_SOURCES,
        },
        null,
        2,
      ),
    );
    return;
  }
  if (args.length !== 3 || args[0] !== '--execute' || args[1] !== '--output')
    throw new Error('Usage: content:collect [--execute --output DIRECTORY]');
  const directory = resolve(args[2]!);
  await mkdir(directory, { recursive: true });
  const report = await collectCandidates();
  await writeFile(resolve(directory, 'candidates.json'), JSON.stringify(report, null, 2) + '\n');
  const failures = report.regions.flatMap((region) =>
    (['official', 'photos'] as const)
      .filter((kind) => region[kind].status !== 'collected')
      .map((kind) => `${region.region}/${kind}`),
  );
  await writeFile(
    resolve(directory, 'README.md'),
    `# Weekly research candidates\n\nCollected: ${report.collectedAt}\n\nPublication: pending human review; no runtime changes.\n\nRegions: ${report.regions.length}. Sources needing attention: ${failures.length}.\n\n${failures.map((item) => '- ' + item).join('\n')}\n\nRead candidates.json. A collection timestamp is not a source publication date or field check.\n`,
  );
  console.log(
    `Candidate artifact written. ${report.regions.length} regions; ${failures.length} source failures. No publishing or image downloads.`,
  );
  if (failures.length) process.exitCode = 1;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  await main().catch((error: unknown) => {
    console.error(error instanceof Error ? error.message : 'Collection failed');
    process.exitCode = 1;
  });
}
