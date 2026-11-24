import { mkdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { searchOpenverse } from '../images/openverse.ts';
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
  const source = DISCOVERY_SOURCES.find((item) => item.url === url);
  if (!source) throw new Error('source_not_allowed');
  if ('manualReview' in source) throw new Error(source.manualReview);
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
    const official =
      'manualReview' in source
        ? { status: 'manual_review' as const, url: source.url, reason: source.manualReview }
        : await collectOfficial(source.url, fetcher).then(
            (data) => ({ status: 'collected' as const, data }),
            (error: unknown) => ({
              status: 'needs_attention' as const,
              error: error instanceof Error ? error.message : 'source_failed',
            }),
          );
    const photos = await searchCommons(query, fetcher).then(
      (data) => ({
        status: data.candidates.length ? ('collected' as const) : ('empty' as const),
        data,
      }),
      (error: unknown) => ({
        status: 'needs_attention' as const,
        error: error instanceof Error ? error.message : 'photos_failed',
      }),
    );
    const openverse =
      photos.status === 'empty' && (source.region === 'bali' || source.region === 'hawaii')
        ? await searchOpenverse(query, fetcher).then(
            (data) => ({
              status: data.candidates.length ? ('collected' as const) : ('empty' as const),
              data,
            }),
            (error: unknown) => ({
              status: 'needs_attention' as const,
              error: error instanceof Error ? error.message : 'openverse_failed',
            }),
          )
        : undefined;
    regions.push({ region: source.region, official, photos, openverse });
  }
  return {
    schema: 2,
    collectedAt: now.toISOString(),
    reviewStatus: 'pending' as const,
    approvedForPublication: false as const,
    notice:
      'Research candidates only. Source titles and links are not verified travel advice. Photo location, attribution, license and depicted-person restrictions require human review. Never copy community posts into this artifact. No automatic publication.',
    regions,
  };
}

export function summarizeCandidates(report: Awaited<ReturnType<typeof collectCandidates>>) {
  const { regions } = report;
  const manual = regions.filter((r) => r.official.status === 'manual_review').length;
  const official = regions.filter((r) => r.official.status === 'collected').length;
  const photoChecks = regions.filter((r) => r.photos.status !== 'needs_attention').length;
  const photoCount = regions.reduce(
    (total, { photos }) =>
      photos.status === 'needs_attention' ? total : total + photos.data.candidates.length,
    0,
  );
  const failed = official === 0 || photoChecks === 0;
  // Only display known diagnostic codes in Markdown. Raw diagnostics remain in the JSON artifact.
  const diagnostic = (message: string) =>
    /^[a-z_0-9]{1,64}$|^Commons HTTP \d{3}; no automatic retry\.$/.test(message)
      ? message
      : 'see candidates.json';
  const extraPhotos = regions.reduce(
    (sum, { openverse }) =>
      sum +
      (openverse && openverse.status !== 'needs_attention' ? openverse.data.candidates.length : 0),
    0,
  );
  const rows = regions.map(({ region, official, photos, openverse }) => {
    const page =
      official.status === 'manual_review'
        ? `[manual_review](${official.url}) (${official.reason})`
        : official.status === 'needs_attention'
          ? `needs_attention (${diagnostic(official.error)})`
          : 'collected';
    const photo =
      photos.status === 'needs_attention'
        ? `needs_attention (${diagnostic(photos.error)})`
        : `${photos.status} (${photos.data.candidates.length})`;
    const extra = !openverse
      ? '—'
      : openverse.status === 'needs_attention'
        ? `needs_attention (${diagnostic(openverse.error)})`
        : `${openverse.status} (${openverse.data.candidates.length})`;
    return `| ${region} | ${page} | ${photo} | ${extra} |`;
  });
  const markdown = [
    '# Weekly research candidates',
    '',
    `Collected: ${report.collectedAt}`,
    '',
    `Official pages collected: ${official} of ${regions.length - manual} automatic. Manual review: ${manual}.`,
    `Photo candidates: ${photoCount + extraPhotos} (Commons: ${photoCount}, Openverse: ${extraPhotos}). Successful Commons searches: ${photoChecks} of ${regions.length}.`,
    ...(failed ? ['', 'Action required: an entire automatic source category is unavailable.'] : []),
    '',
    '| Region | Official page | Commons | Openverse fallback |',
    '| --- | --- | --- | --- |',
    ...rows,
    '',
    'Pending human review. See candidates.json for source links and license details. Collection time is not a publication date or field check.',
    '',
  ].join('\n');
  return { markdown, failed };
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
  const summary = summarizeCandidates(report);
  await writeFile(resolve(directory, 'README.md'), summary.markdown);
  console.log(summary.markdown);
  if (summary.failed) process.exitCode = 1;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  await main().catch((error: unknown) => {
    console.error(error instanceof Error ? error.message : 'Collection failed');
    process.exitCode = 1;
  });
}
