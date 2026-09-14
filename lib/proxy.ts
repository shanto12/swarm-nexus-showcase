import { API_SECURITY_HEADERS } from './security.ts';

// Both hosting surfaces share this boundary. Jobs execute on the isolated AWS service.
export async function proxy(
  request: Request,
  transport: 'aws' | 'netlify' = 'aws',
): Promise<Response> {
  const url = new URL(request.url);
  const match =
    /^\/api\/(health|session|login|logout|tools|assess|tasks(?:\/[a-f0-9]{32}(?:\/(?:pause|resume|cancel|steer|budget|resources|artifacts\/[a-f0-9]{32}))?)?)$/.test(
      url.pathname,
    );
  const safeHeaders = API_SECURITY_HEADERS;
  if (!match)
    return Response.json(
      { detail: 'Not found' },
      { status: 404, headers: safeHeaders },
    );
  if (!['GET', 'POST'].includes(request.method))
    return new Response(null, { status: 405, headers: safeHeaders });
  if (
    request.method === 'POST' &&
    (request.headers.get('origin') !== url.origin ||
      request.headers.get('sec-fetch-site') === 'cross-site')
  )
    return Response.json(
      { detail: 'Request origin is not allowed.' },
      { status: 403, headers: safeHeaders },
    );
  if (Number(request.headers.get('content-length') || 0) > 65536)
    return Response.json(
      { detail: 'Request too large' },
      { status: 413, headers: safeHeaders },
    );
  const headers = new Headers();
  for (const name of ['content-type', 'origin', 'idempotency-key']) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  // Do not export Sites sign-in or unrelated browser cookies to another host.
  const session = (request.headers.get('cookie') || '')
    .split(';')
    .map((part) => part.trim())
    .find((part) => /^nexus_session=[A-Za-z0-9._~-]+$/.test(part));
  if (session) headers.set('cookie', session);
  const upstreamBase =
    transport === 'netlify'
      ? (process.env.NEXUS_RELAY_ORIGIN || 'https://relay.example.test')
      : (process.env.NEXUS_RUNTIME_ORIGIN || 'https://runtime.example.test');
  // Workers cannot fetch an IP directly. The Sites server uses our fixed HTTPS
  // Netlify relay; its browser-origin check above must pass before this rewrite.
  if (transport === 'netlify' && request.method === 'POST')
    headers.set('origin', process.env.NEXUS_RELAY_ORIGIN || 'https://relay.example.test');
  let body: ArrayBuffer | undefined;
  if (request.method === 'POST') {
    body = await request.arrayBuffer();
    if (body.byteLength > 65536)
      return Response.json(
        { detail: 'Request too large' },
        { status: 413, headers: safeHeaders },
      );
  }
  try {
    const upstream = await fetch(upstreamBase + url.pathname, {
      method: request.method,
      headers,
      body,
      // Workerd accepts manual/follow but rejects error before sending a request.
      // Never follow redirects with an owner session attached.
      redirect: 'manual',
      signal: AbortSignal.timeout(15000),
    });
    if (upstream.status >= 300 && upstream.status < 400) {
      await upstream.body?.cancel();
      throw new Error('Upstream redirect rejected');
    }
    const output = new Headers(safeHeaders);
    for (const key of ['content-type', 'content-disposition', 'retry-after']) {
      const value = upstream.headers.get(key);
      if (value) output.set(key, value);
    }
    for (const cookie of upstream.headers.getSetCookie()) {
      if (cookie.startsWith('nexus_session='))
        output.append('set-cookie', cookie);
    }
    return new Response(upstream.body, {
      status: upstream.status,
      headers: output,
    });
  } catch {
    return Response.json(
      {
        detail:
          'The cloud runtime is temporarily unavailable. Your saved missions are retained.',
      },
      { status: 503, headers: safeHeaders },
    );
  }
}
