import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import type { Me } from "./types";

// Server components call FastAPI directly and forward the signed-in user's session as a bearer token,
// so every server-side read runs with that user's role and tenant (never a shared service token).
const SERVER_BASE = process.env.API_URL || "http://localhost:8000";

async function session(): Promise<string | undefined> {
  return (await cookies()).get("fl_session")?.value;
}

export type Loaded<T> = { data: T; error: null } | { data: null; error: string };

/** GET as the signed-in user. Missing/expired session -> /login (redirect is called outside any try). */
export async function serverLoad<T>(path: string, nextPath = "/"): Promise<Loaded<T>> {
  const token = await session();
  if (!token) redirect(`/login?next=${encodeURIComponent(nextPath)}`);
  let res: Response;
  try {
    res = await fetch(`${SERVER_BASE}${path}`, { headers: { Authorization: `Bearer ${token}` }, cache: "no-store" });
  } catch (e) {
    return { data: null, error: `API unreachable (${(e as Error).message})` };
  }
  if (res.status === 401) redirect(`/login?next=${encodeURIComponent(nextPath)}`);
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try {
      const d = (await res.json()).detail;
      msg = typeof d === "string" ? d : d?.message ?? msg;
    } catch { /* non-JSON */ }
    return { data: null, error: msg };
  }
  return { data: (await res.json()) as T, error: null };
}

export async function currentUser(): Promise<Me | null> {
  const token = await session();
  if (!token) return null;
  try {
    const r = await fetch(`${SERVER_BASE}/api/v1/auth/me`, { headers: { Authorization: `Bearer ${token}` }, cache: "no-store" });
    return r.ok ? ((await r.json()) as Me) : null;
  } catch {
    return null;
  }
}
