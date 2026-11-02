import type { ExplainComponent } from "@/lib/types";
import { stripMd } from "@/lib/text";

/**
 * The markets behind a score (pmi-api /explain): what each contract is priced
 * at, how relevant the factor judgements made it, and which way a YES moves
 * the index.
 */
export function DriversTable({ components, limit = 8 }: { components: ExplainComponent[]; limit?: number }) {
  if (components.length === 0) {
    return <p className="text-sm text-ink-muted">No components in the latest reading.</p>;
  }
  const rows = [...components]
    .sort((a, b) => b.relevancy - a.relevancy || (b.volume_24h ?? 0) - (a.volume_24h ?? 0))
    .slice(0, limit);

  return (
    <div className="-mx-5 -mb-5 overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-y border-surface-border bg-surface-muted text-ink-muted">
            <th className="eyebrow px-5 py-2.5 text-left">Market</th>
            <th className="eyebrow px-4 py-2.5 text-left">Venue</th>
            <th className="eyebrow px-4 py-2.5 text-right">Price</th>
            <th className="eyebrow w-44 px-4 py-2.5 text-left">Relevancy</th>
            <th className="eyebrow px-5 py-2.5 text-left">YES moves index</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-surface-border">
          {rows.map((c) => (
            <tr key={c.market_id} className="hover:bg-surface-muted/60">
              <td className="px-5 py-3 font-medium text-ink">{stripMd(c.title)}</td>
              <td className="px-4 py-3">
                <span className="rounded-md bg-surface-muted px-2 py-0.5 text-xs capitalize text-ink-muted">
                  {c.venue ?? "—"}
                </span>
              </td>
              <td className="px-4 py-3 text-right font-semibold tabular-nums">
                {c.last_price !== null ? `${Math.round(c.last_price * 100)}%` : "—"}
              </td>
              <td className="px-4 py-3">
                <div className="flex items-center gap-2.5">
                  <div className="h-1.5 w-24 overflow-hidden rounded-full bg-brand-soft">
                    <div
                      className="h-1.5 rounded-full bg-brand"
                      style={{ width: `${Math.round(Math.min(1, c.relevancy) * 100)}%` }}
                    />
                  </div>
                  <span className="text-xs tabular-nums text-ink-muted">{c.relevancy.toFixed(2)}</span>
                </div>
              </td>
              <td className="px-5 py-3">
                {c.direction > 0 ? (
                  <span className="inline-flex items-center gap-1 rounded-full bg-positive-soft px-2 py-0.5 text-xs font-semibold text-[#047857]">
                    ▲ Up
                  </span>
                ) : c.direction < 0 ? (
                  <span className="inline-flex items-center gap-1 rounded-full bg-negative-soft px-2 py-0.5 text-xs font-semibold text-[#B91C1C]">
                    ▼ Down
                  </span>
                ) : (
                  <span className="text-ink-faint">—</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {components.length > rows.length && (
        <p className="border-t border-surface-border px-5 py-2.5 text-xs text-ink-muted">
          Top {rows.length} of {components.length} components by relevancy.
        </p>
      )}
    </div>
  );
}
