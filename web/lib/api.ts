// Browser-side API client. Calls go to /api/v1/* on the dashboard's own origin (next.config.mjs proxies
// them to FastAPI), so the HttpOnly session cookie travels with them. Writes add X-FL-CSRF, a header a
// cross-site form cannot send. Server components use lib/server.ts instead (cookie -> bearer).
export class ApiError extends Error {
  constructor(public status: number, public detail: unknown) {
    super(typeof detail === "object" && detail && "message" in detail ? String((detail as { message: string }).message)
      : typeof detail === "string" ? detail : `HTTP ${status}`);
  }
  get code(): string | undefined {
    return typeof this.detail === "object" && this.detail && "code" in this.detail
      ? String((this.detail as { code: string }).code) : undefined;
  }
}

export async function handle<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail: unknown = res.statusText;
    try { detail = (await res.json()).detail; } catch { /* non-JSON error body */ }
    if (res.status === 401 && typeof window !== "undefined" && !window.location.pathname.startsWith("/login")) {
      window.location.href = `/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`;
    }
    throw new ApiError(res.status, detail);
  }
  return res.json() as Promise<T>;
}

async function send<T>(method: string, path: string, body?: unknown): Promise<T> {
  return handle<T>(await fetch(path, {
    method, credentials: "same-origin", cache: "no-store",
    headers: { "Content-Type": "application/json", ...(method === "GET" ? {} : { "X-FL-CSRF": "1" }) },
    body: body === undefined ? undefined : JSON.stringify(body),
  }));
}

export const apiGet = <T>(path: string) => send<T>("GET", path);
export const apiPost = <T>(path: string, body?: unknown) => send<T>("POST", path, body);
export const apiPut = <T>(path: string, body?: unknown) => send<T>("PUT", path, body);
export const apiPatch = <T>(path: string, body?: unknown) => send<T>("PATCH", path, body);
