import { describe, it, expect } from 'vitest';

import type { Book, Release } from '../types';
import { buildReleaseDownloadPayload } from '../utils/releasePayload';

const book: Book = {
  id: 'hc-1',
  title: 'Drive',
  author: 'James S. A. Corey',
  year: '2012',
  preview: 'https://img/drive.jpg',
  series_name: 'The Expanse',
  series_position: 2.6,
  subtitle: 'An Expanse Short Story',
  provider: 'hardcover',
  provider_id: 'hc-1',
  source: 'direct_download',
};

const release: Release = {
  source: 'audiobookbay',
  source_id: 'abb-1',
  title: 'James S. A. Corey - The Expanse Complete 2.0',
  format: 'm4b',
  language: 'en',
  download_url: 'https://audiobookbay.lu/abss/expanse/',
};

describe('buildReleaseDownloadPayload', () => {
  it('describes the searched book and the chosen release', () => {
    const payload = buildReleaseDownloadPayload(book, release, 'audiobook');
    expect(payload).toMatchObject({
      source: 'audiobookbay',
      source_id: 'abb-1',
      title: 'Drive',
      author: 'James S. A. Corey',
      series_name: 'The Expanse',
      series_position: 2.6,
      language: 'en',
      content_type: 'audiobook',
    });
    expect(payload.multi_book).toBeUndefined();
    expect(payload.book_plan).toBeUndefined();
  });

  it('uses the release title and author for manual books', () => {
    const manual: Book = { ...book, provider: 'manual', title: 'ignored' };
    const withAuthor = { ...release, extra: { author: 'Release Author' } };
    const payload = buildReleaseDownloadPayload(manual, withAuthor, 'audiobook');
    expect(payload.title).toBe(release.title);
    expect(payload.author).toBe('Release Author');
  });

  it('flags a manual multi-book pack', () => {
    const payload = buildReleaseDownloadPayload(book, release, 'audiobook', { multiBook: true });
    expect(payload.multi_book).toBe(true);
    expect(payload.book_plan).toBeUndefined();
  });

  it('attaches the approved book plan', () => {
    const plan = [{ title: 'Leviathan Wakes', series_position: 1, year: 2011, files: ['a.m4b'] }];
    const payload = buildReleaseDownloadPayload(book, release, 'audiobook', {
      multiBook: true,
      bookPlan: plan,
    });
    expect(payload.multi_book).toBe(true);
    expect(payload.book_plan).toEqual(plan);
  });

  it('carries the ISBN from the metadata provider, preferring ISBN-13', () => {
    const with13 = buildReleaseDownloadPayload(
      { ...book, isbn_10: '0316129047', isbn_13: '9780316129044' },
      release,
      'audiobook',
    );
    expect(with13.isbn).toBe('9780316129044');

    const with10 = buildReleaseDownloadPayload(
      { ...book, isbn_10: '0316129047' },
      release,
      'audiobook',
    );
    expect(with10.isbn).toBe('0316129047');

    const without = buildReleaseDownloadPayload(book, release, 'audiobook');
    expect(without.isbn).toBeUndefined();
  });
});
