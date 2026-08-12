import type { ScrapeCandidate } from '../api/scrape';

export interface CandidateDisplayTitles {
  primary: string;
  secondary: string;
}

export function candidateDisplayTitles(localTitle: string, candidate: ScrapeCandidate): CandidateDisplayTitles {
  const aliases = candidateAliases(candidate);
  const titles = uniqueTitles([candidate.title, ...aliases, candidate.original_title]);
  const localMatches = Boolean(localTitle) && titles.some((title) => titlesShareAnimeMovieIdentity(localTitle, title));
  const chineseTitle = titles.find(isChineseTitle) || '';
  const japaneseTitle = uniqueTitles([
    candidate.original_title,
    String(candidate.source_meta?.native_title || ''),
    String(candidate.raw?.anilist?.title?.native || ''),
    ...aliases,
  ]).find(isJapaneseTitle) || '';

  return {
    primary: localMatches && containsHan(localTitle)
      ? localTitle
      : chineseTitle || candidate.title || candidate.original_title || localTitle || '未命名候选',
    secondary: japaneseTitle,
  };
}

export function formatCandidateScore(score: number): string {
  return `${Math.round(Number.isFinite(score) ? score : 0)} 分`;
}

function candidateAliases(candidate: ScrapeCandidate): string[] {
  const sourceAliases = Array.isArray(candidate.source_meta?.title_aliases)
    ? candidate.source_meta.title_aliases
    : [];
  const rawAliases = Array.isArray(candidate.raw?.provider_title_aliases)
    ? candidate.raw.provider_title_aliases
    : [];
  return uniqueTitles([...sourceAliases, ...rawAliases]);
}

function uniqueTitles(values: unknown[]): string[] {
  const result: string[] = [];
  for (const value of values) {
    const title = String(value || '').trim();
    if (title && !result.includes(title)) result.push(title);
  }
  return result;
}

function containsHan(value: string): boolean {
  return /[\u3400-\u9fff]/.test(value);
}

function isChineseTitle(value: string): boolean {
  return containsHan(value) && !/[\u3040-\u30ff]/.test(value);
}

function isJapaneseTitle(value: string): boolean {
  return /[\u3040-\u30ff]/.test(value);
}

function titlesShareAnimeMovieIdentity(left: string, right: string): boolean {
  const leftKey = normalizeIdentityTitle(left);
  const rightKey = normalizeIdentityTitle(right);
  if (!leftKey || !rightKey) return false;
  if (leftKey === rightKey) return true;
  const shorter = leftKey.length <= rightKey.length ? leftKey : rightKey;
  const longer = leftKey.length > rightKey.length ? leftKey : rightKey;
  if (shorter.length >= 5 && longer.includes(shorter) && shorter.length / longer.length >= 0.82) return true;
  const leftSignature = movieDescriptorSignature(leftKey);
  return Boolean(leftSignature && leftSignature === movieDescriptorSignature(rightKey));
}

function normalizeIdentityTitle(value: string): string {
  return String(value || '')
    .normalize('NFKC')
    .toLocaleLowerCase()
    .replace(/the\s*motion\s*picture|劇場版|剧场版|電影版|电影版|映画/gi, 'movie')
    .replace(/[戰場劇]/g, (character) => ({ 戰: '战', 場: '场', 劇: '剧' })[character] || character)
    .replace(/[^0-9a-z\u3400-\u9fff\u3040-\u30ff]+/g, '');
}

function movieDescriptorSignature(normalizedTitle: string): string {
  const marker = 'movie';
  const markerIndex = normalizedTitle.indexOf(marker);
  if (markerIndex < 0) return '';
  const before = normalizedTitle.slice(Math.max(0, markerIndex - 1), markerIndex);
  const after = normalizedTitle.slice(markerIndex + marker.length, markerIndex + marker.length + 1);
  return (before + after).length >= 2 ? `${before}${marker}${after}` : '';
}
