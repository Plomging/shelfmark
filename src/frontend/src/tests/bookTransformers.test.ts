import { describe, it, expect } from 'vitest';

import { isMetadataBook, type Book, type Release } from '../types/index';
import {
  mergeMetadataBook,
  transformMetadataToBook,
  transformReleaseToDirectBook,
  transformSourceRecordToBook,
} from '../utils/bookTransformers';

describe('bookTransformers.transformReleaseToDirectBook', () => {
  it('maps direct release data into the direct-mode book card shape', () => {
    const release: Release = {
      source: 'direct_download',
      source_id: 'md5-123',
      title: 'Example Title',
      format: 'epub',
      language: 'en',
      size: '2 MB',
      info_url: 'https://example.com/md5/md5-123',
      extra: {
        author: 'Example Author',
        year: '2001',
        preview: 'https://example.com/cover.jpg',
        publisher: 'Example Publisher',
        description: 'Example description',
        info: {
          Downloads: ['42'],
        },
      },
    };

    const book = transformReleaseToDirectBook(release);

    expect(book.id).toBe('md5-123');
    expect(book.title).toBe('Example Title');
    expect(book.author).toBe('Example Author');
    expect(book.year).toBe('2001');
    expect(book.language).toBe('en');
    expect(book.format).toBe('epub');
    expect(book.size).toBe('2 MB');
    expect(book.preview).toBe('https://example.com/cover.jpg');
    expect(book.publisher).toBe('Example Publisher');
    expect(book.description).toBe('Example description');
    expect(book.source).toBe('direct_download');
    expect(book.provider).toBe('direct_download');
    expect(book.provider_id).toBe('md5-123');
    expect(book.provider_display_name).toBe('Direct Download');
    expect(book.source_url).toBe('https://example.com/md5/md5-123');
    expect(book.info).toEqual({ Downloads: ['42'] });
    expect(isMetadataBook(book)).toBe(false);
  });
});

describe('bookTransformers.transformSourceRecordToBook', () => {
  it('maps source-native records into source-backed book context', () => {
    const book = transformSourceRecordToBook({
      id: 'md5-456',
      title: 'Record Title',
      source: 'direct_download',
      author: 'Record Author',
      preview: '/api/covers/md5-456',
      year: 1999,
      language: 'en',
      format: 'epub',
      size: '3 MB',
      publisher: 'Record Publisher',
      description: 'Record description',
      info: {
        Downloads: ['64'],
      },
      source_url: 'https://example.com/record/md5-456',
    });

    expect(book.id).toBe('md5-456');
    expect(book.title).toBe('Record Title');
    expect(book.author).toBe('Record Author');
    expect(book.source).toBe('direct_download');
    expect(book.provider).toBe('direct_download');
    expect(book.provider_id).toBe('md5-456');
    expect(book.provider_display_name).toBe('Direct Download');
    expect(book.year).toBe('1999');
    expect(book.preview).toBe('/api/covers/md5-456');
    expect(book.source_url).toBe('https://example.com/record/md5-456');
    expect(book.info).toEqual({ Downloads: ['64'] });
    expect(isMetadataBook(book)).toBe(false);
  });
});

describe('bookTransformers.transformMetadataToBook', () => {
  const metadata = {
    provider: 'hardcover',
    provider_id: '446681',
    title: 'Dungeon Crawler Carl',
    authors: ['Matt Dinniman'],
  };

  it('carries the library ownership flags through to the Book', () => {
    const book = transformMetadataToBook({ ...metadata, library: { ebook: 'owned' as const } });

    expect(book.library).toEqual({ ebook: 'owned' });
  });

  it('leaves library undefined when the API sends none', () => {
    expect(transformMetadataToBook(metadata).library).toBeUndefined();
  });
});

describe('bookTransformers.mergeMetadataBook', () => {
  const searchBook: Book = {
    id: 'babelio:18811',
    title: 'Harry Potter, tome 1',
    author: 'J. K. Rowling',
    preview: 'https://example.com/cover.jpg',
    provider: 'babelio',
    provider_id: '18811',
    display_fields: [{ label: 'Rating', value: '4.5', icon: 'star' }],
  };
  const fullBook: Book = {
    ...searchBook,
    year: '2003',
    publisher: 'Gallimard Jeunesse',
    isbn_13: '9782070518425',
    genres: ['fantasy'],
    series_id: '1',
    series_name: 'Harry Potter',
    series_position: 1,
    series_count: 7,
    description: 'The full summary.',
    display_fields: [
      { label: 'Pages', value: '332', icon: 'book' },
      { label: 'Rating', value: '4.5', icon: 'star' },
    ],
  };

  it('brings the detail fields over the search book', () => {
    const merged = mergeMetadataBook(searchBook, fullBook);

    expect(merged.year).toBe('2003');
    expect(merged.publisher).toBe('Gallimard Jeunesse');
    expect(merged.isbn_13).toBe('9782070518425');
    expect(merged.genres).toEqual(['fantasy']);
    expect(merged.series_name).toBe('Harry Potter');
    expect(merged.series_position).toBe(1);
    expect(merged.series_count).toBe(7);
    expect(merged.description).toBe('The full summary.');
    expect(merged.display_fields).toHaveLength(2);
  });

  it('keeps search values the detail fetch does not carry', () => {
    const merged = mergeMetadataBook(searchBook, {
      ...fullBook,
      display_fields: undefined,
      library: undefined,
    });

    expect(merged.display_fields).toEqual(searchBook.display_fields);
  });

  it('falls back to a minimal detail fetch without losing the search book', () => {
    // id/title/author are required, so a real detail fetch always carries them
    const emptyDetail: Book = {
      id: 'babelio:18811',
      title: 'Harry Potter, tome 1',
      author: 'J. K. Rowling',
    };
    const merged = mergeMetadataBook(searchBook, emptyDetail);

    expect(merged).toEqual(searchBook);
  });
});
