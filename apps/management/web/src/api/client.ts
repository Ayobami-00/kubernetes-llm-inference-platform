import { client } from './generated/client.gen';
export * as endpoints from './generated/sdk.gen';
export type * from './generated/types.gen';
let csrf = '';
export function setCsrf(value: string) {
  csrf = value;
}
client.setConfig({ baseUrl: '', credentials: 'same-origin' });
client.interceptors.request.use((request) => {
  if (!['GET', 'HEAD'].includes(request.method)) {
    if (csrf) request.headers.set('X-CSRF-Token', csrf);
    if (!request.headers.has('Idempotency-Key'))
      request.headers.set('Idempotency-Key', crypto.randomUUID());
  }
  return request;
});
export function message(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === 'string') return error;
  const value = error as {
    error?: { message?: string; details?: { path?: (string | number)[]; message?: string }[] };
  };
  if (!value?.error?.message) return 'The request could not be completed.';
  return (
    value.error.message +
    (value.error.details
      ?.map((detail) => `\n${(detail.path || []).join('.')}: ${detail.message}`)
      .join('') || '')
  );
}
export async function unwrap<T>(promise: Promise<{ data?: T; error?: unknown }>): Promise<T> {
  const result = await promise;
  if (result.error) throw new Error(message(result.error));
  return result.data as T;
}

export const mutationHeaders = () => ({
  'Idempotency-Key': crypto.randomUUID(),
});
