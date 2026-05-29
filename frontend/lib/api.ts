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

// Backend URL — in Docker, this is the service name; locally, it's localhost
function getApiUrl() {
  if (process.env.NEXT_PUBLIC_API_URL) {
    return process.env.NEXT_PUBLIC_API_URL;
  }

  if (typeof window !== "undefined") {
    return `${window.location.protocol}//${window.location.hostname}:8000`;
  }

  return "http://localhost:8000";
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
