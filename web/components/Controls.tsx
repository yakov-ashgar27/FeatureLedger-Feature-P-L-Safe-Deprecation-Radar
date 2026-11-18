"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";

type Option = { value: string; label: string };

export function Segmented({ param, options, value }: { param: string; options: Option[]; value: string }) {
  const router = useRouter();
  const path = usePathname();
  const sp = useSearchParams();
  return (
    <div className="inline-flex rounded-md border border-rule bg-white p-0.5">
      {options.map((o) => (
        <button key={o.value}
          onClick={() => {
            const next = new URLSearchParams(sp?.toString() ?? "");
            next.set(param, o.value);
            router.push(`${path}?${next.toString()}`);
          }}
          className={`rounded px-2.5 py-1 text-xs ${value === o.value ? "bg-ink text-white" : "text-ink-2 hover:bg-[#f3f2ee]"}`}>
          {o.label}
        </button>
      ))}
    </div>
  );
}
