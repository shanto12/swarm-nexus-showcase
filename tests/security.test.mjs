import assert from 'node:assert/strict';
import { test } from 'node:test';
import { BASE_SECURITY_HEADERS, documentSecurity } from '../lib/security.ts';

test('document policy uses a fresh 128-bit nonce in renderer and response headers', () => {
  const nonces = new Set();
  for (let i = 0; i < 64; i++) {
    const { requestHeaders, responseHeaders } = documentSecurity(new Headers());
    const policy = responseHeaders.get('content-security-policy');
    assert.equal(requestHeaders.get('content-security-policy'), policy);
    const nonce = /'nonce-([A-Za-z0-9+/=]+)'/.exec(policy)?.[1];
    assert.ok(nonce);
    assert.equal(Buffer.from(nonce, 'base64').length, 16);
    nonces.add(nonce);
    for (const [name, value] of Object.entries(BASE_SECURITY_HEADERS))
      assert.equal(responseHeaders.get(name), value);
    assert.equal(responseHeaders.get('cache-control'), 'private, no-store');
  }
  assert.equal(nonces.size, 64);
});

test('attacker-supplied policies and nonce cannot choose the renderer nonce', () => {
  const incoming = new Headers({
    'content-security-policy': "script-src 'nonce-attacker'",
    'content-security-policy-report-only': "script-src 'nonce-attacker'",
    'x-nonce': 'attacker',
    cookie: 'nexus_session=test',
  });
  const { requestHeaders, responseHeaders } = documentSecurity(incoming);
  assert.doesNotMatch(
    requestHeaders.get('content-security-policy'),
    /attacker/,
  );
  assert.equal(requestHeaders.get('content-security-policy-report-only'), null);
  assert.equal(requestHeaders.get('x-nonce'), null);
  assert.equal(requestHeaders.get('cookie'), 'nexus_session=test');
  assert.equal(incoming.get('x-nonce'), 'attacker');
  assert.equal(responseHeaders.get('set-cookie'), null);
});

test('production has no inline-script/eval relaxation or broad connection origin', () => {
  const policy = documentSecurity(new Headers()).responseHeaders.get(
    'content-security-policy',
  );
  const script = policy
    .split('; ')
    .find((part) => part.startsWith('script-src '));
  assert.doesNotMatch(script, /unsafe-inline|unsafe-eval|https:|\*/);
  assert.match(policy, /connect-src 'self';/);
  for (const directive of [
    "frame-ancestors 'none'",
    "form-action 'self'",
    "base-uri 'none'",
    "object-src 'none'",
  ])
    assert.ok(policy.includes(directive));
  const dev = documentSecurity(new Headers(), true).responseHeaders.get(
    'content-security-policy',
  );
  assert.match(dev, /'unsafe-eval'/);
  assert.match(dev, /connect-src 'self' ws: wss:/);
});
