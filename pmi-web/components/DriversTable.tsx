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
    <div className="overflow-x-auto rounded-lg border border-surface-border bg-surface">
      <table className="w-full text-sm">
        <thead className="text-xs uppercase tracking-wide text-ink-muted">
          <tr className="border-b border-surface-border">
            <th className="px-4 py-2.5 text-left font-medium">Market</th>
            <th className="px-4 py-2.5 text-left font-medium">Venue</th>
            <th className="px-4 py-2.5 text-right font-medium">Price</th>
            <th className="px-4 py-2.5 text-left font-medium w-40">Relevancy</th>
            <th className="px-4 py-2.5 text-left font-medium">YES moves index</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-surface-border">
          {rows.map((c) => (
            <tr key={c.market_id}>
              <td className="px-4 py-2.5">{stripMd(c.title)}</td>
              <td className="px-4 py-2.5 text-ink-muted">{c.venue ?? "—"}</td>
              <td className="px-4 py-2.5 text-right tabular-nums">
                {c.last_price !== null ? `${Math.round(c.last_price * 100)}%` : "—"}
              </td>
              <td className="px-4 py-2.5">
                <div className="flex items-center gap-2">
                  <div className="h-1.5 w-24 rounded bg-surface-muted">
                    <div
                      className="h-1.5 rounded bg-accent"
                      style={{ width: `${Math.round(Math.min(1, c.relevancy) * 100)}%` }}
                    />
                  </div>
                  <span className="text-xs tabular-nums text-ink-muted">{c.relevancy.toFixed(2)}</span>
                </div>
              </td>
              <td className="px-4 py-2.5">
                {c.direction > 0 ? (
                  <span className="text-emerald-700">▲ up</span>
                ) : c.direction < 0 ? (
                  <span className="text-red-700">▼ down</span>
                ) : (
                  <span className="text-ink-muted">—</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {components.length > rows.length && (
        <p className="border-t border-surface-border px-4 py-2 text-xs text-ink-muted">
          Top {rows.length} of {components.length} components by relevancy.
        </p>
      )}
    </div>
  );
}
