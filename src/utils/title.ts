const RELEASE_NOISE_PATTERNS = [
  /\b(?:1080p|2160p|720p|4k|8k|hdr|hevc|x26[45]|h\.?26[45]|avc|aac|flac|opus|ma10p|hi10p|web-?dl|b[dr]rip|blu-?ray)\b/gi,
  /\b(?:gb|big5|chs|cht|jpn|japanese|sc|tc)\b/gi,
];

export function cleanDisplayTitle(value: string, fallback = '') {
  let text = decodeTitleEntities(String(value || fallback || '')).trim();
  if (!text) return fallback;

  text = text.replace(/\.[a-z0-9]{2,5}$/i, '');
  text = stripReleaseNoiseBrackets(text);
  for (const pattern of RELEASE_NOISE_PATTERNS) {
    text = text.replace(pattern, ' ');
  }

  text = text
    .replace(/[._]+/g, ' ')
    .replace(/[（(]\s*[)\s）]/g, ' ')
    .replace(/\s*[-–—]\s*/g, ' - ')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/^[\s\-_]+|[\s\-_]+$/g, '');

  const episodeMatch = text.match(/(?:S\d{1,2}E\d{1,3}|E\d{1,3}|第\s*\d{1,3}\s*[话集]|(?:^|\s)\d{1,3}(?:\s|$)).*$/i);
  if (episodeMatch && episodeMatch[0].trim().length >= 2) {
    text = episodeMatch[0].trim();
  }

  return text || fallback;
}

function decodeTitleEntities(value: string) {
  return value
    .replace(/&#x([0-9a-f]+);/gi, (_, hex) => String.fromCodePoint(Number.parseInt(hex, 16)))
    .replace(/&#(\d+);/g, (_, code) => String.fromCodePoint(Number.parseInt(code, 10)))
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'")
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&amp;/g, '&');
}

function stripReleaseNoiseBrackets(value: string) {
  return value
    .replace(/\[([^\]]{1,60})\]/g, (match, content) => (
      isReleaseNoiseTag(content) ? ' ' : match
    ))
    .replace(/【([^】]{1,60})】/g, (match, content) => (
      isReleaseNoiseTag(content) ? ' ' : match
    ));
}

function isReleaseNoiseTag(value: string) {
  const text = String(value || '').trim();
  if (!text) return true;

  const compact = text.replace(/[\s._-]+/g, '').toLowerCase();
  if (!compact) return true;
  if (/^\d{1,3}(?:-\d{1,3})?(?:\+\w+)?$/i.test(text)) return true;
  if (/^\d{3,4}p$/i.test(text)) return true;
  if (/^(?:chs|cht|sc|tc|gb|big5|jpn|japanese|简繁|简体|繁体|内封)$/i.test(text)) return true;
  if (/(?:1080p|2160p|720p|4k|8k|hdr|hevc|x26[45]|h\.?26[45]|avc|aac|flac|opus|ma10p|hi10p|web-?dl|web-?rip|b[dr]rip|blu-?ray)/i.test(text)) return true;
  if (/(?:raws?|subs?|fansub|vcb[-_ ]?studio|lolihouse|nekomoe|kissaten|beansub|airota|haruhana|sakurato|sweetsub|dmg)/i.test(text)) return true;
  if (!/[\u4e00-\u9fff]/.test(text) && /[&+]/.test(text)) return true;

  return false;
}
