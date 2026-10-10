// The run-folder contract written by src/rfp_assistant/evaluation/stage_review.py (schema review-run-1) and served
// by review/server.py.

export type Host = { platform: string; machine: string; python: string; cuda: boolean; gpu: string | null };

export type Candidate = {
  id: string;
  label: string;
  ref: string;
  role: "baseline" | "candidate";
  commit: string;
  working_tree: boolean;
  changed_files?: string[];
  status: "complete" | "failed" | "refused" | "stopped";
  reason?: string | null;
  host?: Host | null;
  elapsed_s?: number | null;
  config?: Record<string, unknown> | null;
  variant?: Record<string, unknown> | null;
  ledger?: Ledger | null;
};

/** A candidate's spend as the shared ledger records it (paid stages). */
export type Ledger = { settled_micro_usd: number; attempts: number; open: number; target: string };

export type Stage = "retriever" | "chunking" | "generation" | "ocr";

export type RunListItem = {
  run_id: string;
  stage: Stage;
  created_at: string;
  imported: { imported_at: string } | null;
  decided: boolean;
  candidates: Pick<Candidate, "id" | "label" | "role" | "commit" | "status" | "host">[];
};

export type Passage = {
  rank: number;
  chunk_id: string;
  grade: number;
  doc: string | null;
  section?: string;
  text: string;
  packed: boolean;
};

export type QuestionResult = {
  error?: string;
  ndcg?: number | null;
  hit5?: number | null;
  gold_rank?: number | null;
  sides?: Passage[][];
  side_docs?: (string | null)[];
  fallback?: string | null;
  limitations?: string[];
};

export type Question = {
  key: string;
  id: string;
  population: "dev" | "needle";
  question: string;
  type: string;
  gold: { group_id: string; doc_id: string | null; quotes: string[] }[];
  candidates: Record<string, QuestionResult>;
  differs: boolean;
  outcome_differs?: boolean;
};

export type RetrieverSummary = {
  ndcg5: number | null;
  ndcg5_n: number;
  dev_rows: number;
  needle_hits: number;
  needle_rows: number;
  errors: number;
};

export type RetrieverView = {
  stage: "retriever";
  run_id: string;
  candidates: Candidate[];
  summary: Record<string, RetrieverSummary>;
  populations: Record<string, { dataset: string; rows: number; skipped: number; not_ranked: number }>;
  activation: { run_id: string | null; mode: string; index_version: string; dense_version: string | null };
  questions: Question[];
};

export type Sizes = {
  count: number;
  min?: number;
  p50?: number;
  p90?: number;
  max?: number;
  mean?: number;
  histogram?: number[];
  edges?: number[];
};

export type ChunkingSummary = { sizes: Sizes; tables_kept: number; tables_titled: number; documents: number };

export type DocListItem = {
  n: number;
  doc_id: string;
  title: string | null;
  differing: number;
  boundaries: number;
  tables_titled: number;
  counts: Record<string, number>;
};

export type ChunkingView = {
  stage: "chunking";
  run_id: string;
  candidates: Candidate[];
  summary: Record<string, ChunkingSummary>;
  documents: DocListItem[];
};

export type Cell = { chunk_id: string; tokens: number; type: string; text: string; title_lost: boolean };

export type DocView = {
  n: number;
  doc_id: string;
  title: string | null;
  candidates: Record<string, { sizes: Sizes; titles_lost: string[]; titled: number }>;
  rows: { at: { element: number; row: number; kind: string | null }; cells: Record<string, Cell>; differs: boolean }[];
};

export type Claim = {
  text: string;
  kind: string | null;
  doc: string | null;
  evidence_ids: string[];
  supported: boolean | null; // true: quoted verbatim from its citation; false: every citation rejected; null: unjudged
  links: { evidence_id: string; valid: boolean; verbatim: boolean; grade: number }[];
};

export type Evidence = { doc: string | null; quote: string | null; section: string; text: string | null };

export type Answer = {
  error?: string;
  detail?: string | null;
  outcome?: string;
  passed?: boolean;
  status_ok?: boolean;
  summary?: string;
  claims?: Claim[];
  evidence?: Record<string, Evidence>;
  missing?: { doc_id: string; field: string; reason: string }[];
  validation?: string | null;
  validation_detail?: string | null;
  required?: { claim_id: string; verdict: string }[];
  cost_micro_usd: number;
  latency_ms?: number | null;
  trace_url?: string | null;
};

export type GenerationQuestion = {
  key: string;
  id: string;
  question: string;
  type: string;
  mode: string | null;
  expected_status: string;
  answerability: string;
  docs: string[];
  required: { claim_id: string; text: string; critical: boolean }[];
  candidates: Record<string, Answer>;
  differs: boolean;
};

export type GenerationSummary = {
  rows: number;
  answered: number;
  passed: number;
  required_correct: number;
  required: number;
  claims_supported: number;
  claims_rejected: number;
  claims: number;
  links_invalid: number;
  links: number;
  validation_failures: Record<string, number>;
  validation_failed: number;
  cost_micro_usd: number;
  paid_answers: number;
  latency_ms: { p50: number | null; p95: number | null; n: number };
  not_done: number;
  ledger?: Ledger | null;
};

export type GenerationView = {
  stage: "generation";
  run_id: string;
  candidates: Candidate[];
  summary: Record<string, GenerationSummary>;
  dataset: { dataset: string; rows: number; skipped: number };
  activation: { run_id: string | null; mode: string; index_version: string; dense_version: string | null };
  traced: boolean;
  questions: GenerationQuestion[];
};

export type Read = {
  status: "local" | "remote" | "unresolved" | "unreadable" | "missing";
  text?: string;
  local_text?: string | null;
  reasons?: string[] | null;
  mean_prob?: number | null;
  error?: string | null;
  micro_usd?: number | null;
  diff?: [string, string, string][] | null; // [tag, baseline text, this candidate's text], character runs
};

export type OcrImage = {
  n: number;
  title: string | null;
  doc_id: string | null;
  where: string;
  sample: boolean;
  cached_reasons: string[];
  shown: boolean;
  candidates: Record<string, Read>;
  differs: boolean;
};

export type OcrSummary = {
  images: number;
  read: number;
  unreadable: number;
  flagged: number;
  reread: number;
  unresolved: number;
  not_reached: number;
  spent_micro_usd: number;
  ledger?: Ledger | null;
};

export type OcrView = {
  stage: "ocr";
  run_id: string;
  candidates: Candidate[];
  summary: Record<string, OcrSummary>;
  set: { ocr_version: string; flagged: number; sample: number; seed: number };
  images: OcrImage[];
};

export type RunView = RetrieverView | ChunkingView | GenerationView | OcrView;

export type Decision = {
  chosen: string | null;
  candidates: { id: string; outcome: string; reason: string | null }[];
  notes: { item: string; note: string }[];
};

export type Saved = { markdown: string; json: string; prompt: string };

export async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error((await res.json().catch(() => null))?.error ?? `${res.status}`);
  return res.json() as Promise<T>;
}

export async function postJson<T>(url: string, body: BodyInit, type = "application/json"): Promise<T> {
  const res = await fetch(url, { method: "POST", body, headers: { "Content-Type": type } });
  const data = await res.json().catch(() => null);
  if (!res.ok) throw new Error(data?.error ?? `${res.status}`);
  return data as T;
}

export const short = (commit: string) => commit.slice(0, 7);

export const when = (iso: string) =>
  new Date(iso).toLocaleString("ko-KR", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });

export const usd = (micro: number) => `$${(micro / 1e6).toFixed(micro && micro < 10_000 ? 5 : 4)}`;

export const hostLabel =(h?: Host | null) => (h ? (h.cuda ? (h.gpu ?? "CUDA GPU") : ["CPU", h.machine].filter(Boolean).join(" · ")) : "—");
