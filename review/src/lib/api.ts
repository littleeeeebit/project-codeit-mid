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
  status: "complete" | "failed" | "refused";
  reason?: string | null;
  host?: Host | null;
  elapsed_s?: number | null;
  config?: Record<string, unknown> | null;
  variant?: Record<string, unknown> | null;
};

export type RunListItem = {
  run_id: string;
  stage: "retriever" | "chunking";
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

export type RunView = RetrieverView | ChunkingView;

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

export const hostLabel = (h?: Host | null) => (h ? (h.cuda ? (h.gpu ?? "CUDA GPU") : `CPU · ${h.machine}`) : "—");
