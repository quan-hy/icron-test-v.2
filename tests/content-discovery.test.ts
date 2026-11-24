import assert from 'node:assert/strict';
import test from 'node:test';
import {
  collectCandidates,
  collectOfficial,
  officialMetadata,
  summarizeCandidates,
} from '../tools/content/collect.ts';
import { boundedText, permitsResearch } from '../tools/content/research-http.ts';
import { DISCOVERY_SOURCES } from '../tools/content/sources.ts';
import { searchCommons } from '../tools/images/search.ts';

test('research respects applicable robot restrictions and fails before fetching blocked pages', async () => {
  assert.equal(
    permitsResearch(
      'User-agent: *\nDisallow: /private*\nAllow: /private/public',
      '/private/public',
    ),
    false,
  );
  assert.equal(permitsResearch('User-agent: OtherBot\nDisallow: /', '/beach'), true);
  assert.equal(
    permitsResearch('User-agent: IvySurfResearchBot\nDisallow: /*secret$', '/beach/secret'),
    false,
  );
  assert.equal(permitsResearch('User-agent: *\nCrawl-delay: 10', '/beach'), false);
  let requests = 0;
  const fetcher: typeof fetch = async () => {
    requests++;
    return new Response('User-agent: *\nDisallow: /', {
      headers: { 'content-type': 'text/plain' },
    });
  };
  await assert.rejects(collectOfficial('https://example.com/', fetcher), /source_not_allowed/);
  assert.equal(requests, 0);
  await assert.rejects(
    collectOfficial(DISCOVERY_SOURCES[0].url, fetcher),
    /robots_requires_manual_review/,
  );
  assert.equal(requests, 1);
});

test('source discovery keeps bounded same-origin link metadata without copying bodies', async () => {
  const result = officialMetadata(
    '<title>Coast guide</title><p>Unpublished body</p><a href="/beach">Local beach</a><a href="/beach#map">Beach map</a><a href="https://other.test/beach">Beach</a><a href="/login?next=beach">Beach sign in</a>',
    'https://official.test/surf',
  );
  assert.equal(result.title, 'Coast guide');
  assert.deepEqual(result.related, [{ title: 'Beach map', url: 'https://official.test/beach' }]);
  assert.doesNotMatch(JSON.stringify(result), /Unpublished body|other.test|sign in/);
  await assert.rejects(boundedText(new Response('x'.repeat(21)), 20), /response_too_large/);
});

test('Commons research accepts only supported photo licenses and never downloads an image', async () => {
  let requests = 0;
  const result = await searchCommons('Raglan beach', async (input, init) => {
    requests++;
    assert.equal(new URL(String(input)).searchParams.get('gsrlimit'), '6');
    assert.equal(init?.redirect, 'error');
    return Response.json({
      query: {
        pages: ['CC BY 4.0', 'CC BY-NC 4.0', 'Unknown'].map((license) => ({
          title: license,
          imageinfo: [
            {
              url: 'https://upload.wikimedia.org/photo.jpg',
              descriptionurl: 'https://commons.wikimedia.org/wiki/File:Photo.jpg',
              width: 1200,
              height: 800,
              extmetadata: { LicenseShortName: { value: license } },
            },
          ],
        })),
      },
    });
  });
  assert.equal(requests, 1);
  assert.equal(result.candidates.length, 1);
  assert.equal(result.candidates[0]?.license, 'CC BY 4.0');
});

test('weekly report preserves partial failures and leaves every region pending publication', async () => {
  const fetcher: typeof fetch = async (input) => {
    const url = new URL(String(input));
    if (url.pathname === '/robots.txt') return new Response('', { status: 404 });
    if (url.hostname === 'commons.wikimedia.org') return Response.json({ query: { pages: [] } });
    if (url.href === DISCOVERY_SOURCES[0].url) return new Response('', { status: 503 });
    return new Response('<title>Official beach guide</title>', {
      headers: { 'content-type': 'text/html' },
    });
  };
  const report = await collectCandidates(new Date('2026-11-03T00:00:00Z'), fetcher);
  assert.equal(report.reviewStatus, 'pending');
  assert.equal(report.approvedForPublication, false);
  assert.equal(report.regions.length, 8);
  assert.equal(report.regions[0]?.official.status, 'needs_attention');
  const manual = report.regions.filter((r) => r.official.status === 'manual_review');
  assert.deepEqual(
    manual.map((r) => r.region),
    ['california', 'tahiti'],
  );
  const summary = summarizeCandidates(report);
  assert.equal(summary.failed, false);
  assert.match(summary.markdown, /Official pages collected: 5 of 6 automatic/);
  assert.match(summary.markdown, /Photo candidates: 0/);
  assert.match(summary.markdown, /bali.*source_http_503/);
  assert.match(summary.markdown, /california.*manual_review/);
  for (const r of manual) {
    const url = DISCOVERY_SOURCES.find((s) => s.region === r.region)!.url;
    await assert.rejects(
      collectOfficial(url, () => assert.fail('manual source requested')),
      /manual_review/,
    );
  }
  assert.equal(
    report.regions.every((region) => region.photos.status === 'empty'),
    true,
  );
});

test('an outage of either automatic source category fails even when the other works', async () => {
  for (const broken of ['photos', 'official']) {
    const report = await collectCandidates(new Date(), async (input) => {
      const url = new URL(String(input));
      if (url.pathname === '/robots.txt') return new Response('', { status: 404 });
      const photos = url.hostname === 'commons.wikimedia.org';
      if (photos === (broken === 'photos')) return new Response('', { status: 503 });
      return photos
        ? Response.json({ query: { pages: [] } })
        : new Response('<title>Official beach</title>', {
            headers: { 'content-type': 'text/html' },
          });
    });
    assert.equal(summarizeCandidates(report).failed, true, broken);
  }
});
