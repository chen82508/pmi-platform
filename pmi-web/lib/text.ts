/** Strip markdown emphasis markers (`**bold**`, `__bold__`) that some venues
 *  (Kalshi, Manifold) leave in raw market titles. We render titles as plain
 *  text, so the markers would otherwise show as literal asterisks — and they're
 *  often unpaired junk (e.g. "13:**"), so we drop the markers rather than try to
 *  render bold. Use everywhere a raw contract/market title is displayed. */
export function stripMd(s: string): string {
  return s.replace(/\*\*/g, "").replace(/__/g, "");
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** Render an `as_of` instant the way a reader says it out loud — "Aug 11, 02:49
 *  UTC" — rather than the machine form ("2026-08-11 02:49Z") the pages grew
 *  independently. Always UTC (never the viewer's locale): a reading is stamped
 *  by the pipeline in UTC, and shifting it per-visitor would make two people
 *  quoting the same score disagree about when it was taken. Use this everywhere
 *  a timestamp is shown so the whole site reads the same. */
export function formatAsOf(iso: string | number | Date): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const hh = String(d.getUTCHours()).padStart(2, "0");
  const mm = String(d.getUTCMinutes()).padStart(2, "0");
  return `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}, ${hh}:${mm} UTC`;
}

/** Compact USD book size — "$1.4B" / "$25.9M" / "$45.2k" / "$940" — the one
 *  place a dollar amount is formatted, for every surface (cards, list-tables,
 *  seat boards, the landing/about injectors).
 *
 *  Five near-copies of this used to live next to their call sites, and they had
 *  drifted apart: two stopped at the k tier, so a $25.9M book printed as
 *  "$25900.5k"; two spelled the thousands suffix "K" while the rest wrote "k";
 *  and the seat board divided by 1000 with no lower bound, printing a $50 book
 *  as "$0k". Scaled tiers keep one decimal (never "$26k" for two different
 *  books an order of a thousand apart) and sub-$1k amounts stay whole dollars.
 *
 *  Returns `null` — not "$0" and not "—" — when there is no book to report, so
 *  each caller decides how absence reads: a table cell wants an em dash
 *  (`?? "—"`), a card or an injected landing block wants to disappear entirely.
 *  A non-positive volume is absence, not a zero worth printing: it means the
 *  venue reported nothing, which after the 2026-08-10 `volume_24h` fix is a
 *  real and honest state (the column is NULL rather than a lifetime total). */
export function formatVolume(n: number | null | undefined): string | null {
  // null / unknown → null (renders as "—"); a genuine 0 is DISTINCT and reads
  // "$0" (a market that traded nothing in 24h is not the same as one we have no
  // 24h figure for — Polymarket's gamma omits volume24hr for many low-activity
  // markets, so those stay NULL; Kalshi always reports, 0 included).
  if (n == null || !Number.isFinite(n) || n < 0) return null;
  if (n === 0) return "$0";
  // Thresholds sit just under each round tier because the tier is chosen before
  // rounding: a plain `n >= 1_000_000` test sends 999,999 down the k branch,
  // which then rounds up to "$1000.0k" — the same "$25900.5k" wart one tier
  // lower. Promoting at 999.95× hands those to the next suffix instead.
  if (n >= 999_950_000) return `$${(n / 1_000_000_000).toFixed(1)}B`;
  if (n >= 999_950) return `$${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 999.5) return `$${(n / 1_000).toFixed(1)}k`;
  return `$${Math.round(n)}`;
}
