/**
 * API Client — Fetch wrapper for the backend.
 *
 * This module centralizes all backend HTTP calls. Instead of using
 * fetch() directly in components, use these functions.
 *
 * Usage:
 *   import { fetchHealth } from "@/lib/api";
 *   const health = await fetchHealth();
 */

// Backend URL — use browser's own hostname so it works with any access method
function getApiUrl() {
  if (typeof window !== "undefined") {
    const host = window.location.hostname === "localhost" ? "127.0.0.1" : window.location.hostname;
    return `${window.location.protocol}//${host}:8000`;
  }

  return "http://127.0.0.1:8000";
}

const REQUEST_TIMEOUT_MS = 10_000;
const SANDBOX_TIMEOUT_MS = 210_000;

async function fetchJson<T>(
  path: string,
  options: RequestInit = {},
  timeoutMs = REQUEST_TIMEOUT_MS,
  errorPrefix = "Request failed",
): Promise<T> {
  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), timeoutMs);

  try {
    const response = await fetch(`${getApiUrl()}${path}`, {
      ...options,
      signal: controller.signal,
    });

    if (!response.ok) {
      const detail = await readErrorDetail(response);
      throw new Error(`${errorPrefix}: ${response.status} ${detail}`);
    }

    return response.json();
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") {
      throw new Error(`${errorPrefix}: timed out after ${Math.round(timeoutMs / 1000)}s`);
    }
    throw err;
  } finally {
    window.clearTimeout(timeoutId);
  }
}

async function readErrorDetail(response: Response) {
  const text = await response.text();
  if (!text) {
    return response.statusText;
  }

  try {
    const data = JSON.parse(text) as { detail?: unknown };
    if (typeof data.detail === "string") {
      return data.detail;
    }
  } catch {
    // Fall through to the raw body.
  }

  return text;
}

/**
 * Health check response shape.
 */
export interface HealthResponse {
  status: string;
  version: string;
}

export type SandboxDialect = "postgres" | "oracle";

export interface SandboxCheckResponse {
  dialect: SandboxDialect;
  ok: boolean;
  container_id: string | null;
  timings_ms: Record<string, number>;
  resource_limits: {
    cpu_quota?: number;
    cpu_period?: number;
    memory_bytes?: number;
    read_only_rootfs?: boolean;
    security_opt?: string[];
  };
  health: boolean;
  ddl_ok: boolean;
  rows: Record<string, string | number | boolean | null>[];
  explain_rows: number;
  destroyed: boolean;
  orphan_containers: string[];
  note: string | null;
}

export interface SandboxRunResponse {
  dialect: SandboxDialect;
  ok: boolean;
  container_id: string | null;
  timings_ms: Record<string, number>;
  resource_limits: SandboxCheckResponse["resource_limits"];
  statements: string[];
  executed: {
    statement: string;
    kind: "query" | "statement";
    rows: number | null;
  }[];
  rows: Record<string, string | number | boolean | null>[];
  explain_rows: number;
  destroyed: boolean;
  orphan_containers: string[];
}

/**
 * Fetch the backend health status.
 *
 * Calls GET /health and returns the response.
 * Throws if the backend is unreachable or returns an error.
 */
export async function fetchHealth(): Promise<HealthResponse> {
  return fetchJson<HealthResponse>("/health", {}, REQUEST_TIMEOUT_MS, "Health check failed");
}

export async function runSandboxCheck(
  dialect: SandboxDialect,
): Promise<SandboxCheckResponse> {
  return fetchJson<SandboxCheckResponse>(
    "/sandbox/check",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dialect }),
    },
    SANDBOX_TIMEOUT_MS,
    "Sandbox check failed",
  );
}

export async function runSandboxSql(
  dialect: SandboxDialect,
  sql: string,
  explain: boolean,
): Promise<SandboxRunResponse> {
  return fetchJson<SandboxRunResponse>(
    "/sandbox/run",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dialect, sql, explain }),
    },
    SANDBOX_TIMEOUT_MS,
    "SQL run failed",
  );
}

// ---------------------------------------------------------------------------
// Schema API
// ---------------------------------------------------------------------------

export interface SchemaColumn {
  name: string;
  dtype: string;
  nullable: boolean;
}

export interface SchemaTable {
  table_name: string;
  columns: SchemaColumn[];
  row_count: number;
}

export interface SchemaResponse {
  tables: SchemaTable[];
  description: string;
  ingested_at: string | null;
}

export async function getSchema(): Promise<SchemaResponse> {
  return fetchJson<SchemaResponse>("/schema", {}, REQUEST_TIMEOUT_MS, "Fetch schema failed");
}

// ---------------------------------------------------------------------------
// Dataset API
// ---------------------------------------------------------------------------

export interface ColumnMetadata {
  name: string;
  dtype: string;
  nullable: boolean;
}

export interface DatasetUploadResponse {
  dataset_id: string;
  session_id: string;
  filename: string;
  table_name: string;
  columns: ColumnMetadata[];
  row_count: number;
  status: string;
  suggested_prompts: string[];
  created_at: string | null;
}

export interface DatasetInfo {
  dataset_id: string;
  filename: string;
  table_name: string;
  dialect: string;
  columns: ColumnMetadata[];
  row_count: number | null;
  status: string;
  suggested_prompts: string[];
  created_at: string | null;
}

export async function uploadDataset(
  sessionId: string,
  file: File,
  dialect: SandboxDialect,
): Promise<DatasetUploadResponse> {
  const formData = new FormData();
  formData.append("file", file);
  formData.append("dialect", dialect);

  const response = await fetch(`${getApiUrl()}/sessions/${sessionId}/datasets`, {
    method: "POST",
    body: formData,
  });

  if (!response.ok) {
    const detail = await readErrorDetail(response);
    throw new Error(`Dataset upload failed: ${response.status} ${detail}`);
  }

  return response.json();
}

export async function listDatasets(
  sessionId: string,
): Promise<DatasetInfo[]> {
  return fetchJson<DatasetInfo[]>(
    `/sessions/${sessionId}/datasets`,
    {},
    REQUEST_TIMEOUT_MS,
    "List datasets failed",
  );
}

// ---------------------------------------------------------------------------
// NL → SQL session/request API (Phase 4)
// ---------------------------------------------------------------------------

export interface CreateSessionResponse {
  session_id: string;
  dialect: string;
  status: string;
  created_at: string;
}

export interface CreateNLResponse {
  request_id: string;
  session_id: string;
  question: string;
  query_sql: string;
  confidence: number | null;
  rationale: string | null;
  execution_results: Record<string, unknown>[];
  execution_rows: number;
  execution_ms: number | null;
  status: string;
  error_message: string | null;
  created_at: string | null;
}

export interface IterationDetail {
  iteration_id: string;
  attempt_number: number;
  status: string;
  generated_sql: string;
  confidence: number | null;
  rationale: string | null;
  execution_results: Record<string, unknown>[] | null;
  execution_rows: number | null;
  execution_ms: number | null;
  error_message: string | null;
  feedback_action: string | null;
  feedback_comment: string | null;
  is_manual_edit?: boolean;
  created_at: string | null;
}

export interface GetRequestResponse {
  request_id: string;
  session_id: string;
  question: string;
  generated_sql: string;
  confidence: number | null;
  rationale: string | null;
  execution_results: Record<string, unknown>[] | null;
  execution_rows: number | null;
  execution_ms: number | null;
  status: string;
  error_message: string | null;
  request_status: string | null;
  iterations: IterationDetail[];
  created_at: string | null;
}

export interface FeedbackResponse {
  action: string;
  status: string;
  request_status: string;
  iteration_id: string;
  attempt_number: number;
  query_sql: string;
  confidence: number | null;
  rationale: string | null;
  execution_results: Record<string, unknown>[];
  execution_rows: number;
  execution_ms: number | null;
  error_message: string | null;
  needs_human_intervention: boolean;
  is_manual_edit?: boolean;
  latency_ms: number | null;
}

const NL_TIMEOUT_MS = 180_000;

export async function createSession(
  dialect: SandboxDialect,
): Promise<CreateSessionResponse> {
  return fetchJson<CreateSessionResponse>(
    "/sessions",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dialect }),
    },
    10_000,
    "Session creation failed",
  );
}

export async function submitNLRequest(
  sessionId: string,
  prompt: string,
  dialect: SandboxDialect,
): Promise<CreateNLResponse> {
  return fetchJson<CreateNLResponse>(
    `/sessions/${sessionId}/requests`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt, dialect }),
    },
    NL_TIMEOUT_MS,
    "NL request failed",
  );
}

export async function getRequestDetails(
  sessionId: string,
  requestId: string,
): Promise<GetRequestResponse> {
  return fetchJson<GetRequestResponse>(
    `/sessions/${sessionId}/requests/${requestId}`,
    {},
    10_000,
    "Fetch request details failed",
  );
}

export async function submitFeedback(
  sessionId: string,
  iterationId: string,
  action: "approve" | "reject" | "edit",
  options?: { comment?: string; editedSql?: string },
): Promise<FeedbackResponse> {
  return fetchJson<FeedbackResponse>(
    `/sessions/${sessionId}/feedback`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        iteration_id: iterationId,
        action,
        comment: options?.comment,
        edited_sql: options?.editedSql,
      }),
    },
    NL_TIMEOUT_MS,
    "Feedback failed",
  );
}
