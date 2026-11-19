"use client";

import { useEffect, useState } from "react";
import { ErrorBox, SectionTitle } from "@/components/ui";
import { apiGet, apiPost } from "@/lib/api";
import { pct, usdShort } from "@/lib/format";
import type { GuardComment, GuardPreview } from "@/lib/types";

const EXAMPLE = ["services/export/pdf_v1/render.py", "services/export/pdf_v1/api.py",
  "services/integrations/zapier_v1/hooks.py", "services/editor/handlers/export.py"].join("\n");

export default function ZombieGuardPage() {
  const [paths, setPaths] = useState(EXAMPLE);
  const [res, setRes] = useState<GuardPreview | null>(null);
  const [comments, setComments] = useState<GuardComment[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => { apiGet<GuardComment[]>("/api/v1/zombie-guard/comments").then(setComments).catch(() => undefined); }, []);

  const run = async () => {
    setBusy(true); setErr(null);
    try {
      const list = paths.split(/\s+/).map((p) => p.trim()).filter(Boolean);
      setRes(await apiPost<GuardPreview>("/api/v1/zombie-guard/preview", { paths: list }));
    } catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
  };

  return (
    <div className="mx-auto max-w-[1180px] space-y-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Zombie-Guard</h1>
        <p className="text-sm text-ink-3">
          When a pull request is opened or pushed to, FeatureLedger maps its changed files through the Feature Graph. If it
          touches a zombie or a feature being sunset, it posts one non-blocking comment with the feature&apos;s 3-month net P&amp;L,
          adoption and sunset stage, and edits that same comment on every push. FeatureLedger&apos;s own cleanup PRs are skipped.
        </p>
      </header>
      {err && <ErrorBox error={err} />}
      <div className="grid gap-6 lg:grid-cols-2">
        <section className="card p-5">
          <SectionTitle title="Try it on a pull request" sub="Paste the changed file paths (one per line). Nothing is posted." />
          <textarea value={paths} onChange={(e) => setPaths(e.target.value)} rows={8} spellCheck={false}
            className="w-full rounded-md border border-rule bg-white p-2 font-mono text-xs" aria-label="Changed file paths" />
          <button className="btn-primary mt-3" disabled={busy} onClick={run}>{busy ? "Checking…" : "Preview the bot comment"}</button>
          {res && (
            <div className="mt-4 space-y-1.5 text-sm">
              {res.features.map((f) => (
                <div key={f.key} className="flex items-center justify-between gap-2">
                  <span>{f.flagged ? "🧟" : "✓"} <b>{f.name}</b> <span className="text-xs text-ink-3">{f.files} file(s)</span></span>
                  <span className="num text-xs text-ink-2">{usdShort(f.net_3m)} · {pct(f.adoption, 1)} · {f.stage}</span>
                </div>
              ))}
              {res.unmapped_files > 0 && <div className="text-xs text-ink-3">{res.unmapped_files} file(s) map to no feature.</div>}
            </div>
          )}
        </section>
        <section className="card p-5">
          <SectionTitle title="The comment" sub={res ? (res.flagged ? "Posted on opened, edited in place on synchronize" : "Clean: the bot stays silent") : "Run a preview"} />
          {res?.comment_markdown
            ? <pre className="max-h-[420px] overflow-auto whitespace-pre-wrap rounded-md bg-[#f6f5f2] p-3 text-xs" data-testid="guard-comment">{res.comment_markdown}</pre>
            : <div className="text-sm text-ink-3">{res ? "No zombie or retiring feature touched: no comment." : "…"}</div>}
        </section>
      </div>
      <section>
        <SectionTitle title="Recent bot comments" sub="One row per pull request; 'updates' counts in-place edits after pushes" />
        <div className="card divide-y divide-rule">
          {comments.length === 0 && <div className="px-4 py-3 text-sm text-ink-3">No pull request has touched a zombie yet.</div>}
          {comments.map((c) => (
            <details key={`${c.repo}#${c.pr_number}`} className="px-4 py-2.5 text-sm">
              <summary className="cursor-pointer">
                <b>{c.repo}#{c.pr_number}</b> · {c.flagged ? c.features.join(", ") : "resolved"} · {c.updates} update(s) · {c.provider}
                <span className="text-xs text-ink-3"> · {c.updated_at.slice(0, 16).replace("T", " ")}</span>
              </summary>
              <pre className="mt-2 whitespace-pre-wrap rounded-md bg-[#f6f5f2] p-3 text-xs">{c.body}</pre>
            </details>
          ))}
        </div>
      </section>
    </div>
  );
}
