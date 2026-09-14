import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';
import { proxy } from '../lib/proxy.ts';

const originalFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = originalFetch;
});
const site = 'https://workspace.example.test';
const relay = 'https://relay.example.test';
function request(path = '/api/health', options = {}) {
  return new Request(site + path, options);
}

function assertApiHeaders(result) {
  for (const [name, expected] of Object.entries({
    'strict-transport-security': 'max-age=31536000; includeSubDomains',
    'x-content-type-options': 'nosniff',
    'x-frame-options': 'DENY',
    'referrer-policy': 'no-referrer',
    'permissions-policy': 'camera=(), microphone=(), geolocation=()',
    'cache-control': 'no-store',
    'content-security-policy':
      "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
  }))
    assert.equal(result.headers.get(name), expected, name);
}

test('Sites uses only the fixed DNS-backed relay', async () => {
  globalThis.fetch = async (url, init) => {
    assert.equal(url, relay + '/api/health');
    assert.equal(init.redirect, 'manual');
    return Response.json({ status: 'ok' });
  };
  assert.equal((await proxy(request(), 'netlify')).status, 200);
});

test('Netlify uses AWS directly and never loops back into itself', async () => {
  globalThis.fetch = async (url) => {
    assert.equal(url, 'https://runtime.example.test/api/health');
    return Response.json({ status: 'ok' });
  };
  assert.equal((await proxy(request())).status, 200);
});

test('Sites validates browser origin before rewriting origin for the relay', async () => {
  let called = false;
  globalThis.fetch = async () => {
    called = true;
    throw Error('unexpected');
  };
  for (const origin of [null, 'https://attacker.example', relay]) {
    const headers = origin ? { origin } : {};
    assert.equal(
      (
        await proxy(
          request('/api/login', { method: 'POST', headers }),
          'netlify',
        )
      ).status,
      403,
    );
  }
  assert.equal(called, false);
});

test('same-origin POST preserves body and idempotency through both boundaries', async () => {
  let calls = 0;
  globalThis.fetch = async (url, init) => {
    calls++;
    if (url.startsWith(relay)) {
      assert.equal(init.headers.get('origin'), relay);
      return proxy(new Request(url, init));
    }
    assert.equal(url, 'https://runtime.example.test/api/assess');
    assert.equal(init.headers.get('origin'), relay);
    assert.equal(init.headers.get('idempotency-key'), 'test-id');
    assert.equal(new TextDecoder().decode(init.body), '{"prompt":"test"}');
    return Response.json({ agents: 2 });
  };
  const result = await proxy(
    request('/api/assess', {
      method: 'POST',
      headers: { origin: site, 'idempotency-key': 'test-id' },
      body: '{"prompt":"test"}',
    }),
    'netlify',
  );
  assert.deepEqual(await result.json(), { agents: 2 });
  assert.equal(calls, 2);
});

test('only Nexus session cookies cross the boundary in either direction', async () => {
  globalThis.fetch = async (_url, init) => {
    assert.equal(init.headers.get('cookie'), 'nexus_session=abc_123-xyz');
    const headers = new Headers();
    headers.append(
      'set-cookie',
      'nexus_session=updated; HttpOnly; Secure; SameSite=Lax; Path=/',
    );
    headers.append('set-cookie', 'unrelated=value');
    return new Response('ok', { headers });
  };
  const result = await proxy(
    request('/api/session', {
      headers: {
        cookie:
          'sites_identity=private; nexus_session=abc_123-xyz; analytics=private',
      },
    }),
    'netlify',
  );
  assert.deepEqual(result.headers.getSetCookie(), [
    'nexus_session=updated; HttpOnly; Secure; SameSite=Lax; Path=/',
  ]);
});

test('logout deletion cookie survives both hops', async () => {
  let calls = 0;
  globalThis.fetch = async (url, init) => {
    calls++;
    if (url.startsWith(relay)) return proxy(new Request(url, init));
    assert.equal(url, 'https://runtime.example.test/api/logout');
    assert.equal(init.headers.get('origin'), relay);
    return new Response(null, {
      headers: {
        'set-cookie': 'nexus_session=""; Max-Age=0; Path=/; Secure; HttpOnly',
      },
    });
  };
  const result = await proxy(
    request('/api/logout', { method: 'POST', headers: { origin: site } }),
    'netlify',
  );
  assert.match(result.headers.get('set-cookie'), /Max-Age=0/);
  assertApiHeaders(result);
  assert.equal(calls, 2);
});

test('rejects unlisted routes, methods, oversized bodies, and cross-site POST', async () => {
  globalThis.fetch = async () => {
    throw Error('must not fetch');
  };
  assert.equal((await proxy(request('/api/admin'), 'netlify')).status, 404);
  assert.equal(
    (await proxy(request('/api/health', { method: 'DELETE' }), 'netlify'))
      .status,
    405,
  );
  assert.equal(
    (
      await proxy(
        request('/api/assess', {
          method: 'POST',
          headers: { origin: site },
          body: 'a'.repeat(65537),
        }),
        'netlify',
      )
    ).status,
    413,
  );
  assert.equal(
    (
      await proxy(
        request('/api/login', {
          method: 'POST',
          headers: { origin: site, 'sec-fetch-site': 'cross-site' },
        }),
        'netlify',
      )
    ).status,
    403,
  );
});

test('upstream failure is sanitized with no-store security headers', async () => {
  globalThis.fetch = async () => {
    throw Error('sensitive provider details');
  };
  const result = await proxy(request(), 'netlify');
  assert.equal(result.status, 503);
  assert.equal(result.headers.get('cache-control'), 'no-store');
  assert.equal(result.headers.get('x-content-type-options'), 'nosniff');
  assert.doesNotMatch(await result.text(), /sensitive/);
});

test('rejects redirects without following them or forwarding Location', async () => {
  let calls = 0;
  globalThis.fetch = async (_url, init) => {
    calls++;
    assert.equal(init.redirect, 'manual');
    return new Response(null, {
      status: 307,
      headers: { location: 'https://untrusted.example' },
    });
  };
  const result = await proxy(request(), 'netlify');
  assert.equal(result.status, 503);
  assert.equal(result.headers.get('location'), null);
  assert.equal(calls, 1);
  assertApiHeaders(result);
});

test('both boundaries enforce full headers on success and every locally generated error', async () => {
  for (const transport of ['aws', 'netlify']) {
    globalThis.fetch = async () => Response.json({ status: 'ok' });
    const success = await proxy(request(), transport);
    assert.equal(success.status, 200);
    assertApiHeaders(success);
    globalThis.fetch = async () => {
      throw Error('unavailable');
    };
    const cases = [
      [request('/api/admin'), 404],
      [request('/api/health', { method: 'DELETE' }), 405],
      [request('/api/login', { method: 'POST' }), 403],
      [
        request('/api/assess', {
          method: 'POST',
          headers: { origin: site, 'content-length': '65537' },
        }),
        413,
      ],
      [
        request('/api/assess', {
          method: 'POST',
          headers: { origin: site },
          body: 'a'.repeat(65537),
        }),
        413,
      ],
      [request(), 503],
    ];
    for (const [input, status] of cases) {
      const result = await proxy(input, transport);
      assert.equal(result.status, status);
      assertApiHeaders(result);
    }
  }
});

test('artifact bytes and attachment headers survive both hops without relaxing policy', async () => {
  let calls = 0;
  const bytes = new Uint8Array([
    0, 10, 60, 115, 99, 114, 105, 112, 116, 62, 255,
  ]);
  const path = `/api/tasks/${'a'.repeat(32)}/artifacts/${'b'.repeat(32)}`;
  globalThis.fetch = async (url, init) => {
    calls++;
    if (url.startsWith(relay)) return proxy(new Request(url, init));
    assert.equal(url, 'https://runtime.example.test' + path);
    return new Response(bytes, {
      headers: {
        'content-type': 'application/octet-stream',
        'content-disposition': 'attachment; filename="test.html"',
        'strict-transport-security': 'max-age=0',
        'x-frame-options': 'SAMEORIGIN',
        'permissions-policy': 'camera=*',
        'cache-control': 'public, max-age=86400',
        'content-security-policy': "script-src 'unsafe-inline'",
        'access-control-allow-origin': '*',
      },
    });
  };
  const result = await proxy(request(path), 'netlify');
  assert.equal(result.status, 200);
  assert.equal(calls, 2);
  assertApiHeaders(result);
  assert.equal(result.headers.get('access-control-allow-origin'), null);
  assert.equal(result.headers.get('content-type'), 'application/octet-stream');
  assert.equal(
    result.headers.get('content-disposition'),
    'attachment; filename="test.html"',
  );
  assert.deepEqual(new Uint8Array(await result.arrayBuffer()), bytes);
});
