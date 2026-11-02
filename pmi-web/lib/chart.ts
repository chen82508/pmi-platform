/** Padded [min, max] around the scores, clamped to the [0, 100] index range.
 *  A fixed 0–100 axis flattens a two-week move of a couple of points into a
 *  straight line; the padding keeps small moves from looking dramatic. */
export function fittedDomain(scores: number[]): [number, number] {
  if (scores.length === 0) return [0, 100];
  const lo = Math.min(...scores);
  const hi = Math.max(...scores);
  const pad = Math.max((hi - lo) * 0.25, 1);
  return [Math.max(0, Math.floor(lo - pad)), Math.min(100, Math.ceil(hi + pad))];
}
