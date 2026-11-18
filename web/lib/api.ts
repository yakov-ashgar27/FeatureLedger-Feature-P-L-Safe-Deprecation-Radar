// Server components call FastAPI directly (API_URL); the browser calls the same paths on the
// dashboard origin, which next.config.mjs rewrites to FastAPI.
const SERVER_BASE = process.env.API_URL || "http://localhost:8000";

function headers(): HeadersInit {
  const h: Record<string, string> = { "Content-Type": "application/json" };
  if (typeof window === "undefined" && process.env.FL_API_TOKEN) h.Authorization = `Bearer ${process.env.FL_API_TOKEN}`;
  return h;
}

export class ApiError extends Error {
  constructor(public status: number, public detail: unknown) {
    super(typeof detail === "object" && detail && "message" in detail ? String((detail as { message: string }).message)
      : typeof detail === "string" ? detail : `HTTP ${status}`);
  }
}

async function handle<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail: unknown = res.statusText;
    try { detail = (await res.json()).detail; } catch { /* non-JSON error body */ }
    throw new ApiError(res.status, detail);
  }
  return res.json() as Promise<T>;
}

export async function apiGet<T>(path: string): Promise<T> {
  const base = typeof window === "undefined" ? SERVER_BASE : "";
  return handle<T>(await fetch(`${base}${path}`, { headers: headers(), cache: "no-store" }));
}

export async function apiPost<T>(path: string, body?: unknown): Promise<T> {
  const base = typeof window === "undefined" ? SERVER_BASE : "";
  return handle<T>(await fetch(`${base}${path}`, {
    method: "POST", headers: headers(), body: body === undefined ? undefined : JSON.stringify(body), cache: "no-store",
  }));
}
