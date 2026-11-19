import { SectionTitle } from "@/components/ui";
import { GLOSSARY, type Term } from "@/lib/glossary";

const GROUPS: { key: Term["group"]; title: string; sub: string }[] = [
  { key: "Money", title: "The money", sub: "What each column of the ledger means" },
  { key: "Usage", title: "Usage", sub: "How much the feature is used, and how much of its effort is bug fixing" },
  { key: "Classes", title: "Classes", sub: "The label each feature gets, and why" },
  { key: "Trust", title: "How sure is the number?", sub: "Where the data comes from, and when to double-check" },
  { key: "Sunset", title: "Retiring a feature safely", sub: "The steps and the guards" },
];

export default function GlossaryPage() {
  const terms = Object.entries(GLOSSARY);
  return (
    <div className="mx-auto max-w-[900px] space-y-8">
      <div>
        <h1 className="text-2xl font-semibold">Glossary</h1>
        <p className="mt-1 text-sm text-ink-2">Every number and label in FeatureLedger, in plain words. Hover a column header or badge
          anywhere in the app to see the short version.</p>
      </div>
      {GROUPS.map((g) => (
        <section key={g.key}>
          <SectionTitle title={g.title} sub={g.sub} />
          <dl className="card divide-y divide-rule">
            {terms.filter(([, t]) => t.group === g.key).map(([k, t]) => (
              <div key={k} id={k} className="px-4 py-3">
                <dt className="font-medium">{t.term}</dt>
                <dd className="mt-0.5 text-sm text-ink-2">{t.short}</dd>
                <dd className="mt-1 text-xs text-ink-3">{t.long}</dd>
              </div>
            ))}
          </dl>
        </section>
      ))}
    </div>
  );
}
