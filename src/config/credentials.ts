export const TMDB_API_SETTINGS_URL = 'https://www.themoviedb.org/settings/api';
export const BANGUMI_ACCESS_TOKEN_URL = 'https://next.bgm.tv/demo/access-token';

/** 识别用户最容易误粘贴的 TMDB v3 API 密钥，并给出可操作的纠正提示。 */
export function getTmdbCredentialError(value: string): string {
  const token = value.trim();
  if (!token) return '';
  if (/^[a-f0-9]{32}$/i.test(token)) {
    return '你粘贴的是 32 位 API 密钥；KumiPlayer 需要上方较长的“API 读取访问令牌”。';
  }
  return '';
}
