let apiSessionToken = ''

export function setApiSessionToken(token: string): void {
  apiSessionToken = token.trim()
}

export function getApiSessionToken(): string {
  return apiSessionToken
}

export function apiSessionHeaders(): Record<string, string> {
  return apiSessionToken
    ? { 'X-KumiPlayer-Token': apiSessionToken }
    : {}
}

export function withApiSessionToken(url: string): string {
  if (!apiSessionToken) return url
  const separator = url.includes('?') ? '&' : '?'
  return `${url}${separator}api_token=${encodeURIComponent(apiSessionToken)}`
}
