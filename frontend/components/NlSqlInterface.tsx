"use client";

import { useEffect, useState } from "react";

import {
  createSession,
  getSchema,
  submitNLRequest,
  type CreateNLResponse,
  type SandboxDialect,
  type SchemaTable,
} from "@/lib/api";

interface SchemaInfo {
  name: string;
  rows: number;
  columns: number;
}

export default function NlSqlInterface() {
  const [prompt, setPrompt] = useState("");
  const [dialect, setDialect] = useState<SandboxDialect>("postgres");
  const [result, setResult] = useState<CreateNLResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isRunning, setIsRunning] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [schemaTables, setSchemaTables] = useState<SchemaInfo[]>([]);
  const [schemaLoaded, setSchemaLoaded] = useState(false);

  useEffect(() => {
    getSchema()
      .then((schema) => {
        const tables = schema.tables.map((t: SchemaTable) => ({
          name: t.table_name,
          rows: t.row_count,
          columns: t.columns.length,
        }));
        setSchemaTables(tables);
        setSchemaLoaded(true);
      })
      .catch(() => {
        setSchemaLoaded(true);
      });
  }, []);

  async function ensureSession() {
    if (sessionId) return sessionId;

    const session = await createSession(dialect);
    setSessionId(session.session_id);
    return session.session_id;
  }

  async function handleSubmit() {
    if (!prompt.trim()) return;

    setResult(null);
    setError(null);
    setIsRunning(true);

    try {
      const sid = await ensureSession();
      const response = await submitNLRequest(sid, prompt, dialect);
      setResult(response);
    } catch (err) {
      setError(err instanceof Error ? err.message : "NL request failed");
    } finally {
      setIsRunning(false);
    }
  }

  return (
    <section className="w-full max-w-5xl space-y-5">
      <div className="border-b border-gray-700 pb-4">
        <h2 className="text-2xl font-semibold text-white">Natural Language to SQL</h2>
        <p className="mt-1 text-sm text-gray-400">
          Datasets are preloaded from the workspace. Ask a question in plain English.
        </p>
      </div>

      {schemaLoaded && schemaTables.length > 0 ? (
        <div className="space-y-2 rounded-md border border-emerald-700 bg-emerald-950/30 p-3 text-xs text-emerald-200">
          <strong>Available tables ({schemaTables.length}):</strong>
          <div className="grid grid-cols-1 gap-1 sm:grid-cols-2 lg:grid-cols-3">
            {schemaTables.map((t) => (
              <div key={t.name} className="truncate">
                <code className="text-cyan-300">{t.name}</code>
                <span className="text-emerald-400">
                  {" "}
                  ({t.rows.toLocaleString()} rows, {t.columns} cols)
                </span>
              </div>
            ))}
          </div>
        </div>
      ) : schemaLoaded && schemaTables.length === 0 ? (
        <div className="rounded-md border border-yellow-700 bg-yellow-950/30 p-3 text-xs text-yellow-200">
          No tables found. Place CSV files in the project workspace and restart the backend.
        </div>
      ) : (
        <div className="rounded-md border border-gray-700 bg-gray-900 p-3 text-xs text-gray-400">
          Loading schema...
        </div>
      )}

      <div className="grid gap-4 lg:grid-cols-[1fr_auto]">
        <div className="space-y-3">
          <label className="text-sm font-semibold text-gray-200" htmlFor="nl-prompt">
            What data do you need?
          </label>
          <textarea
            className="min-h-28 w-full resize-y rounded-md border border-gray-700 bg-gray-950 p-3 font-mono text-sm text-gray-100 outline-none transition placeholder:text-gray-600 focus:border-cyan-500"
            disabled={isRunning}
            id="nl-prompt"
            onChange={(event) => setPrompt(event.target.value)}
            placeholder="e.g. Show the first 10 rows from consolidated_fbdi"
            spellCheck={false}
            value={prompt}
          />
        </div>

        <div className="flex flex-col gap-3 lg:items-end lg:justify-end">
          <div className="flex rounded-md border border-gray-700 p-1">
            {(["postgres", "oracle"] as const).map((d) => (
              <button
                key={d}
                className={`px-4 py-2 text-sm font-medium capitalize transition ${
                  dialect === d
                    ? "bg-cyan-500 text-gray-950"
                    : "text-gray-300 hover:bg-gray-800"
                }`}
                disabled={isRunning}
                onClick={() => setDialect(d)}
                type="button"
              >
                {d}
              </button>
            ))}
          </div>
          <button
            className="w-full rounded-md bg-emerald-500 px-6 py-3 text-sm font-semibold text-gray-950 transition hover:bg-emerald-400 disabled:cursor-not-allowed disabled:bg-gray-700 disabled:text-gray-400 lg:w-auto"
            disabled={isRunning || !prompt.trim()}
            onClick={handleSubmit}
            type="button"
          >
            {isRunning ? "Processing..." : "Generate & Run"}
          </button>
        </div>
      </div>

      {error ? <ErrorState message={error} /> : null}
      {isRunning ? <RunningState /> : null}
      {result ? <ResultView result={result} /> : null}
      {!error && !result && !isRunning ? <EmptyState /> : null}
    </section>
  );
}

function EmptyState() {
  return (
    <div className="flex min-h-48 items-center justify-center rounded-md border border-dashed border-gray-700 text-sm text-gray-500">
      Enter a natural language question and click &quot;Generate &amp; Run&quot;
    </div>
  );
}

function RunningState() {
  return (
    <div className="flex min-h-48 flex-col items-center justify-center gap-3 rounded-md border border-gray-700 bg-gray-900 text-sm text-gray-400">
      <div className="h-8 w-8 animate-spin rounded-full border-2 border-gray-700 border-t-cyan-400" />
      <span>Calling LLM, validating SQL, executing query...</span>
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

function ResultView({ result }: { result: CreateNLResponse }) {
  const isSuccess = result.status === "completed";

  return (
    <div className="space-y-4 rounded-md border border-gray-700 bg-gray-900 p-4">
      <div className="flex items-center justify-between">
        <div>
          <div className="text-lg font-semibold text-white">
            {isSuccess ? "Query executed" : "Execution failed"}
          </div>
          <div className="text-xs text-gray-500">
            Confidence: {result.confidence != null ? `${(result.confidence * 100).toFixed(0)}%` : "N/A"}
            {result.execution_ms != null ? ` | ${result.execution_ms.toFixed(0)} ms` : ""}
          </div>
        </div>
        <span
          className={`w-fit rounded px-2 py-1 text-xs font-semibold ${
            isSuccess ? "bg-emerald-500 text-gray-950" : "bg-red-500 text-white"
          }`}
        >
          {result.status}
        </span>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Block title="Generated Query SQL" language="sql" code={result.query_sql} />

        {result.rationale ? (
          <Block title="Rationale" code={result.rationale} />
        ) : null}
      </div>

      {isSuccess && result.execution_results.length > 0 ? (
        <DataTable
          title={`Execution Results (${result.execution_rows} rows)`}
          data={result.execution_results}
        />
      ) : null}

      {result.error_message ? (
        <div className="rounded-md border border-red-800 bg-red-950/40 p-3 text-xs text-red-200">
          {result.error_message}
        </div>
      ) : null}
    </div>
  );
}

function Block({
  title,
  code,
  language,
}: {
  title: string;
  code: string;
  language?: string;
}) {
  return (
    <div className="rounded-md border border-gray-700 bg-gray-950 p-3">
      <div className="mb-2 text-xs font-semibold uppercase text-gray-500">{title}</div>
      <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words rounded bg-gray-800 p-2 font-mono text-xs text-gray-200">
        {code}
      </pre>
    </div>
  );
}

function DataTable({
  title,
  data,
}: {
  title: string;
  data: Record<string, unknown>[];
}) {
  if (data.length === 0) return null;
  const columns = Object.keys(data[0]);

  return (
    <div className="rounded-md border border-gray-700 bg-gray-950 p-3">
      <div className="mb-2 text-xs font-semibold uppercase text-gray-500">{title}</div>
      <div className="max-h-72 overflow-auto">
        <table className="w-full border-collapse text-xs">
          <thead>
            <tr className="border-b border-gray-700">
              {columns.map((col) => (
                <th key={col} className="px-2 py-1 text-left font-semibold text-gray-400">
                  {col}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {data.map((row, i) => (
              <tr key={i} className="border-b border-gray-800">
                {columns.map((col) => (
                  <td key={col} className="px-2 py-1 text-gray-300">
                    {String(row[col] ?? "NULL")}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
