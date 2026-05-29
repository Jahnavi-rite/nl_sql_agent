"use client";

import { useState } from "react";

import {
  runSandboxCheck,
  runSandboxSql,
  type SandboxCheckResponse,
  type SandboxDialect,
  type SandboxRunResponse,
} from "@/lib/api";

const dialects: SandboxDialect[] = ["postgres", "oracle"];
const sampleSql: Record<SandboxDialect, string> = {
  postgres:
    "CREATE TABLE items (id INT PRIMARY KEY, name TEXT);\nINSERT INTO items VALUES (1, 'alpha'), (2, 'beta');\nSELECT id, name FROM items ORDER BY id;",
  oracle:
    "CREATE TABLE items (id NUMBER PRIMARY KEY, name VARCHAR2(20));\nINSERT INTO items VALUES (1, 'alpha');\nINSERT INTO items VALUES (2, 'beta');\nSELECT id, name FROM items ORDER BY id;",
};

export default function SandboxDiagnostics() {
  const [activeDialect, setActiveDialect] = useState<SandboxDialect>("postgres");
  const [result, setResult] = useState<SandboxCheckResponse | null>(null);
  const [sqlResult, setSqlResult] = useState<SandboxRunResponse | null>(null);
  const [sql, setSql] = useState(sampleSql.postgres);
  const [explain, setExplain] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isRunning, setIsRunning] = useState(false);

  function handleDialectChange(dialect: SandboxDialect) {
    setActiveDialect(dialect);
    setSql(sampleSql[dialect]);
    setResult(null);
    setSqlResult(null);
    setError(null);
  }

  async function handleRun(dialect: SandboxDialect) {
    setActiveDialect(dialect);
    setResult(null);
    setSqlResult(null);
    setError(null);
    setIsRunning(true);

    try {
      const response = await runSandboxCheck(dialect);
      setResult(response);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sandbox check failed");
    } finally {
      setIsRunning(false);
    }
  }

  async function handleRunSql() {
    setResult(null);
    setSqlResult(null);
    setError(null);
    setIsRunning(true);

    try {
      const response = await runSandboxSql(activeDialect, sql, explain);
      setSqlResult(response);
    } catch (err) {
      setError(err instanceof Error ? err.message : "SQL run failed");
    } finally {
      setIsRunning(false);
    }
  }

  return (
    <section className="w-full max-w-5xl space-y-5">
      <div className="flex flex-col gap-3 border-b border-gray-700 pb-4 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h2 className="text-2xl font-semibold text-white">Sandbox Diagnostics</h2>
          <p className="mt-1 text-sm text-gray-400">
            Run a real container lifecycle smoke test against the backend.
          </p>
        </div>

        <div className="flex rounded-md border border-gray-700 p-1">
          {dialects.map((dialect) => (
            <button
              key={dialect}
              className={`px-4 py-2 text-sm font-medium capitalize transition ${
                activeDialect === dialect
                  ? "bg-cyan-500 text-gray-950"
                  : "text-gray-300 hover:bg-gray-800"
              }`}
              disabled={isRunning}
              onClick={() => handleDialectChange(dialect)}
              type="button"
            >
              {dialect}
            </button>
          ))}
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-[220px_1fr]">
        <div className="space-y-3">
          <button
            className="w-full rounded-md bg-cyan-500 px-4 py-3 text-sm font-semibold text-gray-950 transition hover:bg-cyan-400 disabled:cursor-not-allowed disabled:bg-gray-700 disabled:text-gray-400"
            disabled={isRunning}
            onClick={() => handleRun(activeDialect)}
            type="button"
          >
            {isRunning ? "Running..." : `Run ${activeDialect} check`}
          </button>

          <div className="rounded-md border border-gray-700 bg-gray-900 p-3 text-xs text-gray-400">
            Oracle can take around 90 seconds on a cold start.
          </div>
        </div>

        <div className="space-y-4 rounded-md border border-gray-700 bg-gray-900 p-4">
          <div className="space-y-3">
            <div className="flex items-center justify-between gap-3">
              <label className="text-sm font-semibold text-gray-200" htmlFor="sandbox-sql">
                SQL script
              </label>
              <label className="flex items-center gap-2 text-xs text-gray-400">
                <input
                  checked={explain}
                  className="h-4 w-4 accent-cyan-500"
                  onChange={(event) => setExplain(event.target.checked)}
                  type="checkbox"
                />
                Explain final query
              </label>
            </div>
            <textarea
              className="min-h-44 w-full resize-y rounded-md border border-gray-700 bg-gray-950 p-3 font-mono text-sm text-gray-100 outline-none transition placeholder:text-gray-600 focus:border-cyan-500"
              disabled={isRunning}
              id="sandbox-sql"
              onChange={(event) => setSql(event.target.value)}
              spellCheck={false}
              value={sql}
            />
            <button
              className="rounded-md bg-emerald-500 px-4 py-2 text-sm font-semibold text-gray-950 transition hover:bg-emerald-400 disabled:cursor-not-allowed disabled:bg-gray-700 disabled:text-gray-400"
              disabled={isRunning || !sql.trim()}
              onClick={handleRunSql}
              type="button"
            >
              {isRunning ? "Running..." : "Run SQL in sandbox"}
            </button>
          </div>

          {error ? <ErrorState message={error} /> : null}
          {!error && !result && !sqlResult && !isRunning ? <EmptyState /> : null}
          {isRunning ? <RunningState dialect={activeDialect} /> : null}
          {result ? <ResultView result={result} /> : null}
          {sqlResult ? <SqlResultView result={sqlResult} /> : null}
        </div>
      </div>
    </section>
  );
}

function SqlResultView({ result }: { result: SandboxRunResponse }) {
  const memoryMb = result.resource_limits.memory_bytes
    ? Math.round(result.resource_limits.memory_bytes / 1024 / 1024)
    : null;

  return (
    <div className="space-y-4 border-t border-gray-700 pt-4">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <div className="text-lg font-semibold text-white">SQL run complete</div>
          <div className="text-xs text-gray-500">
            {result.statements.length} statement(s), {result.timings_ms.total.toLocaleString()} ms
          </div>
        </div>
        <span
          className={`w-fit rounded px-2 py-1 text-xs font-semibold ${
            result.ok ? "bg-emerald-500 text-gray-950" : "bg-red-500 text-white"
          }`}
        >
          {result.ok ? "Clean teardown" : "Check cleanup"}
        </span>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Metric label="Dialect" value={result.dialect} />
        <Metric label="Rows" value={String(result.rows.length)} />
        <Metric label="Explain rows" value={String(result.explain_rows)} />
        <Metric label="Memory" value={memoryMb ? `${memoryMb} MB` : "-"} />
      </div>

      <div className="grid gap-3 lg:grid-cols-2">
        <DataBlock title="Returned Rows" data={result.rows} />
        <DataBlock title="Executed Statements" data={result.executed} />
        <DataBlock
          title="Cleanup"
          data={{
            container_id: result.container_id,
            destroyed: result.destroyed,
            orphan_containers: result.orphan_containers,
          }}
        />
        <DataBlock title="Resource Limits" data={result.resource_limits} />
      </div>
    </div>
  );
}

function EmptyState() {
  return (
    <div className="flex min-h-64 items-center justify-center text-sm text-gray-500">
      Select a dialect and run the check.
    </div>
  );
}

function RunningState({ dialect }: { dialect: SandboxDialect }) {
  return (
    <div className="flex min-h-64 flex-col items-center justify-center gap-3 text-sm text-gray-400">
      <div className="h-8 w-8 animate-spin rounded-full border-2 border-gray-700 border-t-cyan-400" />
      <span>Creating an isolated {dialect} sandbox...</span>
    </div>
  );
}

function ErrorState({ message }: { message: string }) {
  return (
    <div className="rounded-md border border-red-800 bg-red-950/40 p-4 text-sm text-red-200">
      {message}
    </div>
  );
}

function ResultView({ result }: { result: SandboxCheckResponse }) {
  const memoryMb = result.resource_limits.memory_bytes
    ? Math.round(result.resource_limits.memory_bytes / 1024 / 1024)
    : null;

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <div className="text-lg font-semibold capitalize text-white">
            {result.dialect} check {result.ok ? "passed" : "failed"}
          </div>
          <div className="text-xs text-gray-500">
            Completed in {result.timings_ms.total.toLocaleString()} ms
          </div>
        </div>
        <span
          className={`w-fit rounded px-2 py-1 text-xs font-semibold ${
            result.ok ? "bg-emerald-500 text-gray-950" : "bg-red-500 text-white"
          }`}
        >
          {result.ok ? "OK" : "Needs attention"}
        </span>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Metric label="Health" value={result.health ? "Pass" : "Fail"} />
        <Metric label="DDL/DML" value={result.ddl_ok ? "Pass" : "Fail"} />
        <Metric label="Destroyed" value={result.destroyed ? "Yes" : "No"} />
        <Metric label="Explain rows" value={String(result.explain_rows)} />
      </div>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Metric label="CPU quota" value={String(result.resource_limits.cpu_quota ?? "-")} />
        <Metric label="CPU period" value={String(result.resource_limits.cpu_period ?? "-")} />
        <Metric label="Memory" value={memoryMb ? `${memoryMb} MB` : "-"} />
        <Metric
          label="Read-only root"
          value={result.resource_limits.read_only_rootfs ? "Yes" : "No"}
        />
      </div>

      <div className="grid gap-3 lg:grid-cols-2">
        <DataBlock title="Query Result" data={result.rows} />
        <DataBlock
          title="Cleanup"
          data={{
            container_id: result.container_id,
            orphan_containers: result.orphan_containers,
          }}
        />
      </div>

      {result.note ? (
        <div className="rounded-md border border-amber-700 bg-amber-950/40 p-3 text-xs text-amber-100">
          {result.note}
        </div>
      ) : null}
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border border-gray-700 bg-gray-950 p-3">
      <div className="text-xs text-gray-500">{label}</div>
      <div className="mt-1 break-words text-sm font-semibold text-gray-100">{value}</div>
    </div>
  );
}

function DataBlock({ title, data }: { title: string; data: unknown }) {
  return (
    <div className="rounded-md border border-gray-700 bg-gray-950 p-3">
      <div className="mb-2 text-xs font-semibold uppercase text-gray-500">{title}</div>
      <pre className="max-h-56 overflow-auto whitespace-pre-wrap break-words text-xs text-gray-300">
        {JSON.stringify(data, null, 2)}
      </pre>
    </div>
  );
}
