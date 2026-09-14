// Keep both hosting surfaces and locally generated API errors equally protected.
export const BASE_SECURITY_HEADERS = Object.freeze({
  'Strict-Transport-Security': 'max-age=31536000; includeSubDomains',
  'X-Content-Type-Options': 'nosniff',
  'X-Frame-Options': 'DENY',
  'Referrer-Policy': 'no-referrer',
  'Permissions-Policy': 'camera=(), microphone=(), geolocation=()',
});

export const API_SECURITY_HEADERS = Object.freeze({
  ...BASE_SECURITY_HEADERS,
  'Cache-Control': 'no-store',
  'Content-Security-Policy':
    "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
});

export function documentSecurity(incoming: Headers, development = false) {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  const nonce = btoa(String.fromCharCode(...bytes));
  const policy = [
    "default-src 'self'",
    `script-src 'self' 'nonce-${nonce}'${development ? " 'unsafe-eval'" : ''}`,
    "style-src 'self' 'unsafe-inline'",
    "font-src 'self'",
    "img-src 'self' data:",
    `connect-src 'self'${development ? ' ws: wss:' : ''}`,
    "frame-ancestors 'none'",
    "form-action 'self'",
    "base-uri 'none'",
    "object-src 'none'",
  ].join('; ');
  const requestHeaders = new Headers(incoming);
  // Vinext reads the request CSP to nonce its SSR and RSC bootstrap scripts.
  // Overwrite untrusted inbound policy/nonce hints; never let a visitor choose it.
  requestHeaders.set('Content-Security-Policy', policy);
  requestHeaders.delete('Content-Security-Policy-Report-Only');
  requestHeaders.delete('x-nonce');
  const responseHeaders = new Headers({
    ...BASE_SECURITY_HEADERS,
    'Content-Security-Policy': policy,
    'Cache-Control': 'private, no-store',
  });
  return { requestHeaders, responseHeaders };
}
