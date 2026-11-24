import { boundedText, researchFetch } from '../content/research-http.ts';
import { isFreePhotoLicense } from './search.ts';

// Keep source-page metadata for human review. Do not fetch images or follow result URLs.
export async function searchOpenverse(query: string, fetcher: typeof fetch = fetch) {
  const url = new URL('https://api.openverse.org/v1/images/');
  url.search = new URLSearchParams({
    q: query,
    license: 'by,cc0',
    excluded_source: 'wikimedia',
    page_size: '6',
  }).toString();
  const response = await researchFetch(url, fetcher);
  if (!response.ok) throw new Error(`openverse_http_${response.status}`);
  if (!response.headers.get('content-type')?.includes('application/json'))
    throw new Error('openverse_invalid_response');
  const body = JSON.parse(await boundedText(response));
  if (!Array.isArray(body?.results)) throw new Error('openverse_invalid_response');

  const seen = new Set<string>();
  const candidates = [];
  for (const row of body.results.slice(0, 6)) {
    if (
      !row ||
      row.mature !== false ||
      typeof row.source !== 'string' ||
      !row.source ||
      row.source === 'wikimedia'
    )
      continue;
    if (row.license !== 'by' && row.license !== 'cc0') continue;
    const by = row.license === 'by';
    const license = `${by ? 'CC BY' : 'CC0'} ${row.license_version}`;
    if (
      !isFreePhotoLicense(license) ||
      (by && (typeof row.creator !== 'string' || !row.creator.trim()))
    )
      continue;
    const licenseUrl = `https://creativecommons.org/${by ? 'licenses/by' : 'publicdomain/zero'}/${row.license_version}/`;
    if (
      typeof row.license_url !== 'string' ||
      row.license_url.replace(/^http:/, 'https:') !== licenseUrl
    )
      continue;
    if (typeof row.foreign_landing_url !== 'string' || !URL.canParse(row.foreign_landing_url))
      continue;
    const source = new URL(row.foreign_landing_url);
    source.hash = '';
    if (!['https:', 'http:'].includes(source.protocol) || source.username || source.password)
      continue;
    if (typeof row.title !== 'string' || !row.title.trim() || seen.has(source.href)) continue;
    seen.add(source.href);
    candidates.push({
      source: source.href,
      title: row.title as string,
      creator: typeof row.creator === 'string' ? row.creator : null,
      provider: row.source as string,
      license,
      licenseUrl,
    });
  }
  return { query, candidates };
}
