/**
 * Shapes mirror `pmi-api/pmi_api/schemas.py` (Pydantic v2). Hand-written;
 * keep this file thin: types only, no runtime logic.
 */

export interface IndexSummary {
  id: string; // Pydantic alias of `index_id`
  version: number;
  title: string;
  is_current: boolean;
  owner: string | null;
  yaml_sha256: string;
  effective_from: string; // ISO 8601
}

export interface ScorePayload {
  index_id: string;
  version: number;
  as_of: string;
  // null when the index couldn't produce a score this tick (below
  // min_components / zero relevancy). UI shows "no data" rather than 0.
  score: number | null;
  // Seats / countries / markets, depending on the formula — NOT a contract
  // count. Use `market_count` for that.
  component_count: number;
  market_count: number;
  // Venues behind this reading's components, most-contributing first.
  venues: string[];
  computed_at: string;
  breakdown: Record<string, unknown> | null;
}

export interface ScoreEnvelope {
  summary: string;
  data: ScorePayload;
}

export interface HistoryPoint {
  as_of: string;
  score: number | null;
  component_count: number;
}

export interface HistoryPayload {
  index_id: string;
  version: number;
  points: HistoryPoint[];
}

export interface HistoryEnvelope {
  summary: string;
  data: HistoryPayload;
}

export interface ExplainComponent {
  market_id: number;
  title: string;
  last_price: number | null;
  relevancy: number;
  direction: number;
  factors: Record<string, number | string | null>;
  venue: string | null;
  volume_24h: number | null;
  // Components sharing an id are the same question on different venues
  // (engine/peer_grouping.py); the holdings UI folds them into one card.
  peer_group_id: number | null;
}

export interface ExplainPayload {
  index_id: string;
  version: number;
  as_of: string;
  score: number | null;
  components: ExplainComponent[];
}

export interface SourceHealthRow {
  source: string;
  status: string;
  last_success_at: string | null;
  last_failure_at: string | null;
  consecutive_failures: number;
  records_24h: number | null;
  expected_records_24h: number | null;
  updated_at: string;
}
