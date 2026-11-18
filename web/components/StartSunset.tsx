"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { apiPost } from "@/lib/api";

export function StartSunset({ featureKey, defaultReason }: { featureKey: string; defaultReason: string }) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState(defaultReason);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (!open) return <button className="btn-primary" onClick={() => setOpen(true)}>Start safe sunset</button>;
  return (
    <div className="card w-[420px] p-4">
      <div className="label mb-2">Why sunset this feature?</div>
      <textarea value={reason} onChange={(e) => setReason(e.target.value)} rows={3}
        className="w-full rounded-md border border-rule bg-white p-2 text-sm outline-none focus:border-ink-3" />
      {err && <div className="mt-2 text-xs text-[#8a3413]">{err}</div>}
      <div className="mt-3 flex justify-end gap-2">
        <button className="btn" onClick={() => setOpen(false)}>Cancel</button>
        <button className="btn-primary" disabled={busy || reason.length < 10}
          onClick={async () => {
            setBusy(true); setErr(null);
            try {
              const plan = await apiPost<{ id: string }>("/api/v1/sunset-plans", { feature_key: featureKey, reason });
              router.push(`/sunset?plan=${plan.id}`);
            } catch (e) { setErr((e as Error).message); setBusy(false); }
          }}>Create plan</button>
      </div>
    </div>
  );
}
