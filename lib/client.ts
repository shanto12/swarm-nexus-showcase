import {SHOWCASE, capturedResponse} from './showcase';
export async function api<T>(
  path: string,
  body?: unknown,
  signal?: AbortSignal,
  key?: string,
): Promise<T> {
  if (SHOWCASE) return await capturedResponse(path, body) as T;
  const response = await fetch('/api' + path, {
    method: body === undefined ? 'GET' : 'POST',
    credentials: 'same-origin',
    cache: 'no-store',
    headers:
      body === undefined
        ? {}
        : {
            'Content-Type': 'application/json',
            ...(key ? { 'Idempotency-Key': key } : {}),
          },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  });
  const data = await response
    .json()
    .catch(() => ({ detail: 'The runtime returned an unreadable response.' }));
  if (!response.ok)
    throw new Error(
      (data as { detail?: string }).detail ||
        'The request could not be completed.',
    );
  return data as T;
}
export const number = (value: number) =>
  new Intl.NumberFormat('en-US', {
    notation: 'compact',
    maximumFractionDigits: 1,
  }).format(value || 0);
export const time = (date: string) =>
  new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/Chicago',
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  }).format(new Date(date));
