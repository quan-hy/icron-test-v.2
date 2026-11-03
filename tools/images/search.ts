// Manual, read-only Wikimedia Commons discovery. Never downloads or publishes a candidate.
import { pathToFileURL } from 'node:url';
import { boundedText, researchFetch } from '../content/research-http.ts';

export function isFreePhotoLicense(license: string): boolean {
  return /^(CC0(?: 1\.0)?|Public domain|CC BY (?:2\.0|3\.0|4\.0))$/i.test(license.trim());
}

export async function searchCommons(query: string, fetcher: typeof fetch = fetch) {
  query = query.trim();
  if (!query || query.length > 200)
    throw new Error('Usage: npm run images:search -- "Raglan beach" (1–200 characters)');
  const url = new URL('https://commons.wikimedia.org/w/api.php');
  url.search = new URLSearchParams({
    action: 'query',
    format: 'json',
    formatversion: '2',
    generator: 'search',
    gsrnamespace: '6',
    gsrsearch: `${query} filetype:bitmap`,
    gsrlimit: '6',
    prop: 'imageinfo',
    iiprop: 'url|size|extmetadata',
  }).toString();
  const response = await researchFetch(url, fetcher);
  if (!response.ok) throw new Error(`Commons HTTP ${response.status}; no automatic retry.`);
  const result = JSON.parse(await boundedText(response)) as {
    error?: { code: string };
    query?: {
      pages?: {
        title: string;
        imageinfo?: {
          url: string;
          descriptionurl: string;
          width: number;
          height: number;
          extmetadata?: Record<string, { value: string }>;
        }[];
      }[];
    };
  };
  if (result.error) throw new Error(`Commons API ${result.error.code}`);
  const candidates = (result.query?.pages ?? []).flatMap((page) => {
    const info = page.imageinfo?.[0];
    const meta = info?.extmetadata;
    const license = meta?.LicenseShortName?.value ?? '';
    if (!info || !isFreePhotoLicense(license)) return [];
    return [
      {
        title: page.title,
        source: info.descriptionurl,
        original: info.url,
        width: info.width,
        height: info.height,
        authorHtml: meta?.Artist?.value,
        creditHtml: meta?.Credit?.value,
        attributionHtml: meta?.Attribution?.value,
        restrictions: meta?.Restrictions?.value,
        license,
        licenseUrl: meta?.LicenseUrl?.value,
      },
    ];
  });
  return {
    query,
    notice:
      'Candidates only. Verify exact location, author, license and depicted-person restrictions on the source page; review the crop before adding assets. Preserve attribution, license link and change notice. No API key, paid plan or runtime calls.',
    candidates,
  };
}

async function main() {
  console.log(JSON.stringify(await searchCommons(process.argv.slice(2).join(' ')), null, 2));
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  await main().catch((error: unknown) => {
    console.error(error instanceof Error ? error.message : 'Photo search failed');
    process.exitCode = 1;
  });
}
