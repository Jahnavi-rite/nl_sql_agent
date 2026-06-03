"use client";

import { useEffect, useState } from "react";

import {
  createSession,
  getRequestDetails,
  getSchema,
  submitFeedback,
  submitNLRequest,
  type CreateNLResponse,
  type FeedbackResponse,
  type IterationDetail,
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

  // Feedback state
  const [requestId, setRequestId] = useState<string | null>(null);
  const [iterations, setIterations] = useState<IterationDetail[]>([]);
  const [requestStatus, setRequestStatus] = useState<string>("open");
  const [feedbackLoading, setFeedbackLoading] = useState(false);
  const [feedbackError, setFeedbackError] = useState<string | null>(null);
  const [editMode, setEditMode] = useState(false);
  const [editedSql, setEditedSql] = useState("");
  const [rejectMode, setRejectMode] = useState(false);
  const [rejectComment, setRejectComment] = useState("");

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

  function resetFeedbackState() {
    setRequestId(null);
    setIterations([]);
    setRequestStatus("open");
    setFeedbackError(null);
    setEditMode(false);
    setEditedSql("");
    setRejectMode(false);
    setRejectComment("");
  }

  function handleDialectChange(d: SandboxDialect) {
    setDialect(d);
    if (d === "oracle") {
      setResult(null);
      setError(null);
      resetFeedbackState();
    }
  }

  async function handleSubmit() {
    if (!prompt.trim()) return;

    setResult(null);
    setError(null);
    resetFeedbackState();
    setIsRunning(true);

    try {
      const sid = await ensureSession();
      const response = await submitNLRequest(sid, prompt, dialect);
      setResult(response);
      setRequestId(response.request_id);

      // Fetch full request details to get iteration history
      if (response.request_id) {
        try {
          const details = await getRequestDetails(sid, response.request_id);
          setIterations(details.iterations || []);
          setRequestStatus(details.request_status || "open");
        } catch {
          // Non-critical: result is already shown
        }
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "NL request failed");
    } finally {
      setIsRunning(false);
    }
  }

  async function refreshRequestDetails() {
    if (!sessionId || !requestId) return;
    try {
      const details = await getRequestDetails(sessionId, requestId);
      setIterations(details.iterations || []);
      setRequestStatus(details.request_status || "open");
    } catch {
      // Non-critical
    }
  }

  async function handleApprove() {
    if (!sessionId || iterations.length === 0) return;
    const latest = iterations[iterations.length - 1];
    setFeedbackLoading(true);
    setFeedbackError(null);
    try {
      const resp = await submitFeedback(sessionId, latest.iteration_id, "approve");
      setRequestStatus(resp.request_status);
      await refreshRequestDetails();
    } catch (err) {
      setFeedbackError(err instanceof Error ? err.message : "Approve failed");
    } finally {
      setFeedbackLoading(false);
    }
  }

  async function handleReject() {
    if (!sessionId || iterations.length === 0) return;
    const latest = iterations[iterations.length - 1];
    setFeedbackLoading(true);
    setFeedbackError(null);
    try {
      const resp = await submitFeedback(
        sessionId,
        latest.iteration_id,
        "reject",
        { comment: rejectComment || undefined },
      );
      setRequestStatus(resp.request_status);
      setRejectMode(false);
      setRejectComment("");
      // Update result with regenerated data
      setResult((prev) =>
        prev
          ? {
              ...prev,
              query_sql: resp.query_sql,
              confidence: resp.confidence,
              rationale: resp.rationale,
              execution_results: resp.execution_results,
              execution_rows: resp.execution_rows,
              execution_ms: resp.execution_ms,
              status: resp.status,
              error_message: resp.error_message,
            }
          : prev,
      );
      await refreshRequestDetails();
    } catch (err) {
      setFeedbackError(err instanceof Error ? err.message : "Reject failed");
    } finally {
      setFeedbackLoading(false);
    }
  }

  async function handleEdit() {
    if (!sessionId || iterations.length === 0 || !editedSql.trim()) return;
    const latest = iterations[iterations.length - 1];
    setFeedbackLoading(true);
    setFeedbackError(null);
    try {
      const resp = await submitFeedback(
        sessionId,
        latest.iteration_id,
        "edit",
        { editedSql },
      );
      setRequestStatus(resp.request_status);
      setEditMode(false);
      // Update result with edited execution data
      setResult((prev) =>
        prev
          ? {
              ...prev,
              query_sql: resp.query_sql,
              confidence: resp.confidence,
              rationale: resp.rationale,
              execution_results: resp.execution_results,
              execution_rows: resp.execution_rows,
              execution_ms: resp.execution_ms,
              status: resp.status,
              error_message: resp.error_message,
            }
          : prev,
      );
      await refreshRequestDetails();
    } catch (err) {
      setFeedbackError(err instanceof Error ? err.message : "Edit failed");
    } finally {
      setFeedbackLoading(false);
    }
  }

  const latestIteration = iterations.length > 0 ? iterations[iterations.length - 1] : null;
  const isApproved = requestStatus === "approved";
  const isCapped = requestStatus === "needs_human_intervention";
  const feedbackDisabled = feedbackLoading || isApproved || isCapped;

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
                onClick={() => handleDialectChange(d)}
                type="button"
              >
                {d}
              </button>
            ))}
          </div>
          {dialect === "oracle" ? (
            <div className="rounded-md border border-amber-700 bg-amber-950/40 p-2 text-xs text-amber-200">
              Oracle support is coming soon. Only PostgreSQL is available at this time.
            </div>
          ) : null}
          <button
            className="w-full rounded-md bg-emerald-500 px-6 py-3 text-sm font-semibold text-gray-950 transition hover:bg-emerald-400 disabled:cursor-not-allowed disabled:bg-gray-700 disabled:text-gray-400 lg:w-auto"
            disabled={isRunning || !prompt.trim() || dialect === "oracle"}
            onClick={handleSubmit}
            type="button"
          >
            {isRunning ? "Processing..." : "Generate & Run"}
          </button>
        </div>
      </div>

      {error ? <ErrorState message={error} /> : null}
      {isRunning ? <RunningState /> : null}
      {result ? (
        <ResultView
          result={result}
          iterations={iterations}
          requestStatus={requestStatus}
          feedbackLoading={feedbackLoading}
          feedbackError={feedbackError}
          feedbackDisabled={feedbackDisabled}
          editMode={editMode}
          editedSql={editedSql}
          rejectMode={rejectMode}
          rejectComment={rejectComment}
          latestIteration={latestIteration}
          onApprove={handleApprove}
          onReject={handleReject}
          onEdit={handleEdit}
          onToggleEdit={() => {
            setEditMode(!editMode);
            if (!editMode && latestIteration) {
              setEditedSql(latestIteration.generated_sql);
            }
            setRejectMode(false);
          }}
          onToggleReject={() => {
            setRejectMode(!rejectMode);
            setEditMode(false);
          }}
          onEditedSqlChange={setEditedSql}
          onRejectCommentChange={setRejectComment}
          onNewQuery={() => {
            setResult(null);
            resetFeedbackState();
          }}
        />
      ) : null}
      {!error && !result && !isRunning ? <EmptyState /> : null}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

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

function ResultView({
  result,
  iterations,
  requestStatus,
  feedbackLoading,
  feedbackError,
  feedbackDisabled,
  editMode,
  editedSql,
  rejectMode,
  rejectComment,
  latestIteration,
  onApprove,
  onReject,
  onEdit,
  onToggleEdit,
  onToggleReject,
  onEditedSqlChange,
  onRejectCommentChange,
  onNewQuery,
}: {
  result: CreateNLResponse;
  iterations: IterationDetail[];
  requestStatus: string;
  feedbackLoading: boolean;
  feedbackError: string | null;
  feedbackDisabled: boolean;
  editMode: boolean;
  editedSql: string;
  rejectMode: boolean;
  rejectComment: string;
  latestIteration: IterationDetail | null;
  onApprove: () => void;
  onReject: () => void;
  onEdit: () => void;
  onToggleEdit: () => void;
  onToggleReject: () => void;
  onEditedSqlChange: (sql: string) => void;
  onRejectCommentChange: (comment: string) => void;
  onNewQuery: () => void;
}) {
  const isSuccess = result.status === "completed" || result.status === "executed";
  const isApproved = requestStatus === "approved";
  const isCapped = requestStatus === "needs_human_intervention";

  return (
    <div className="space-y-4 rounded-md border border-gray-700 bg-gray-900 p-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <div className="text-lg font-semibold text-white">
            {isSuccess ? "Query executed" : "Execution failed"}
          </div>
          <div className="text-xs text-gray-500">
            Confidence: {result.confidence != null ? `${(result.confidence * 100).toFixed(0)}%` : "N/A"}
            {result.execution_ms != null ? ` | ${result.execution_ms.toFixed(0)} ms` : ""}
            {iterations.length > 0 ? ` | Iteration ${iterations.length} of 5` : ""}
          </div>
        </div>
        <div className="flex items-center gap-2">
          <span
            className={`w-fit rounded px-2 py-1 text-xs font-semibold ${
              isSuccess ? "bg-emerald-500 text-gray-950" : "bg-red-500 text-white"
            }`}
          >
            {result.status}
          </span>
          {isApproved ? (
            <span className="rounded bg-green-600 px-2 py-1 text-xs font-semibold text-white">
              Approved
            </span>
          ) : null}
        </div>
      </div>

      {/* Status banners */}
      {isApproved ? (
        <div className="rounded-md border border-green-700 bg-green-950/40 p-3 text-sm text-green-200">
          This query has been approved.
        </div>
      ) : null}

      {isCapped ? (
        <div className="rounded-md border border-amber-700 bg-amber-950/40 p-3 text-sm text-amber-200">
          Maximum iterations reached. Please edit the SQL manually or submit a new question.
        </div>
      ) : null}

      {/* Source indicator */}
      {latestIteration?.is_manual_edit === true ? (
        <div className="rounded-md border border-amber-700 bg-amber-950/30 p-2 text-xs text-amber-200">
          Manually edited SQL — executed without LLM
        </div>
      ) : latestIteration ? (
        <div className="rounded-md border border-cyan-700 bg-cyan-950/20 p-2 text-xs text-cyan-200">
          AI-generated SQL — produced by LLM
        </div>
      ) : null}

      {/* Query and rationale */}
      <div className="grid gap-4 lg:grid-cols-2">
        <Block title={latestIteration?.is_manual_edit === true ? "Edited Query SQL" : "Generated Query SQL"} language="sql" code={result.query_sql} />
        {result.rationale ? <Block title="Rationale" code={result.rationale} /> : null}
      </div>

      {/* Execution results */}
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

      {/* Feedback controls */}
      {!isApproved && !isCapped && latestIteration ? (
        <FeedbackControls
          feedbackLoading={feedbackLoading}
          feedbackDisabled={feedbackDisabled}
          editMode={editMode}
          editedSql={editedSql}
          rejectMode={rejectMode}
          rejectComment={rejectComment}
          feedbackError={feedbackError}
          onApprove={onApprove}
          onReject={onReject}
          onEdit={onEdit}
          onToggleEdit={onToggleEdit}
          onToggleReject={onToggleReject}
          onEditedSqlChange={onEditedSqlChange}
          onRejectCommentChange={onRejectCommentChange}
        />
      ) : null}

      {/* Iteration history */}
      {iterations.length > 1 ? <IterationHistory iterations={iterations} /> : null}

      {/* New query button */}
      <div className="flex justify-end border-t border-gray-700 pt-3">
        <button
          className="rounded-md bg-gray-700 px-4 py-2 text-sm font-medium text-gray-200 transition hover:bg-gray-600"
          onClick={onNewQuery}
          type="button"
        >
          New Query
        </button>
      </div>
    </div>
  );
}

function FeedbackControls({
  feedbackLoading,
  feedbackDisabled,
  editMode,
  editedSql,
  rejectMode,
  rejectComment,
  feedbackError,
  onApprove,
  onReject,
  onEdit,
  onToggleEdit,
  onToggleReject,
  onEditedSqlChange,
  onRejectCommentChange,
}: {
  feedbackLoading: boolean;
  feedbackDisabled: boolean;
  editMode: boolean;
  editedSql: string;
  rejectMode: boolean;
  rejectComment: string;
  feedbackError: string | null;
  onApprove: () => void;
  onReject: () => void;
  onEdit: () => void;
  onToggleEdit: () => void;
  onToggleReject: () => void;
  onEditedSqlChange: (sql: string) => void;
  onRejectCommentChange: (comment: string) => void;
}) {
  return (
    <div className="space-y-3 rounded-md border border-gray-700 bg-gray-950 p-4">
      <div className="text-xs font-semibold uppercase text-gray-500">Feedback</div>

      {/* Action buttons */}
      <div className="flex flex-wrap gap-2">
        <button
          className="rounded-md bg-green-600 px-4 py-2 text-sm font-semibold text-white transition hover:bg-green-500 disabled:cursor-not-allowed disabled:bg-gray-700 disabled:text-gray-400"
          disabled={feedbackDisabled}
          onClick={onApprove}
          type="button"
        >
          {feedbackLoading ? "Processing..." : "Approve"}
        </button>
        <button
          className={`rounded-md px-4 py-2 text-sm font-semibold transition ${
            rejectMode
              ? "bg-red-700 text-white"
              : "bg-red-600 text-white hover:bg-red-500"
          } disabled:cursor-not-allowed disabled:bg-gray-700 disabled:text-gray-400`}
          disabled={feedbackDisabled}
          onClick={onToggleReject}
          type="button"
        >
          Reject
        </button>
        <button
          className={`rounded-md px-4 py-2 text-sm font-semibold transition ${
            editMode
              ? "bg-amber-700 text-white"
              : "bg-amber-600 text-white hover:bg-amber-500"
          } disabled:cursor-not-allowed disabled:bg-gray-700 disabled:text-gray-400`}
          disabled={feedbackDisabled}
          onClick={onToggleEdit}
          type="button"
        >
          Edit SQL
        </button>
      </div>

      {/* Reject panel */}
      {rejectMode ? (
        <div className="space-y-2">
          <textarea
            className="min-h-20 w-full resize-y rounded-md border border-gray-700 bg-gray-900 p-3 text-sm text-gray-100 outline-none transition placeholder:text-gray-600 focus:border-red-500"
            disabled={feedbackLoading}
            onChange={(event) => onRejectCommentChange(event.target.value)}
            placeholder="Describe what's wrong with the current query..."
            value={rejectComment}
          />
          <button
            className="rounded-md bg-red-600 px-4 py-2 text-sm font-semibold text-white transition hover:bg-red-500 disabled:cursor-not-allowed disabled:bg-gray-700"
            disabled={feedbackLoading}
            onClick={onReject}
            type="button"
          >
            {feedbackLoading ? "Regenerating..." : "Regenerate with Feedback"}
          </button>
        </div>
      ) : null}

      {/* Edit panel */}
      {editMode ? (
        <div className="space-y-2">
          <textarea
            className="min-h-32 w-full resize-y rounded-md border border-gray-700 bg-gray-900 p-3 font-mono text-sm text-gray-100 outline-none transition placeholder:text-gray-600 focus:border-amber-500"
            disabled={feedbackLoading}
            onChange={(event) => onEditedSqlChange(event.target.value)}
            spellCheck={false}
            value={editedSql}
          />
          <button
            className="rounded-md bg-amber-600 px-4 py-2 text-sm font-semibold text-white transition hover:bg-amber-500 disabled:cursor-not-allowed disabled:bg-gray-700"
            disabled={feedbackLoading || !editedSql.trim()}
            onClick={onEdit}
            type="button"
          >
            {feedbackLoading ? "Running..." : "Run Edited SQL"}
          </button>
        </div>
      ) : null}

      {/* Feedback error */}
      {feedbackError ? (
        <div className="rounded-md border border-red-800 bg-red-950/40 p-2 text-xs text-red-200">
          {feedbackError}
        </div>
      ) : null}

      {feedbackLoading ? (
        <div className="flex items-center gap-2 text-xs text-gray-400">
          <div className="h-4 w-4 animate-spin rounded-full border-2 border-gray-700 border-t-cyan-400" />
          {rejectMode && !editMode ? "Regenerating SQL with feedback context..." : editMode && !rejectMode ? "Validating and executing edited SQL..." : "Processing feedback..."}
        </div>
      ) : null}
    </div>
  );
}

function IterationHistory({ iterations }: { iterations: IterationDetail[] }) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div className="space-y-2">
      <button
        className="flex w-full items-center justify-between rounded-md border border-gray-700 bg-gray-950 px-4 py-2 text-sm font-medium text-gray-300 transition hover:bg-gray-800"
        onClick={() => setExpanded(!expanded)}
        type="button"
      >
        <span>Iteration History ({iterations.length} attempts)</span>
        <span className="text-xs text-gray-500">{expanded ? "Hide" : "Show"}</span>
      </button>

      {expanded ? (
        <div className="space-y-3">
          {[...iterations].reverse().map((it, idx) => {
            const isLatest = idx === 0;
            const isManual = it.is_manual_edit === true;
            return (
              <div
                key={it.iteration_id}
                className={`rounded-md border p-3 text-xs ${
                  isLatest
                    ? "border-cyan-700 bg-gray-950"
                    : "border-gray-800 bg-gray-950/50"
                }`}
              >
                <div className="mb-2 flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <span className="font-semibold text-gray-200">
                      Attempt {it.attempt_number}
                    </span>
                    <StatusBadge status={it.status} />
                    {isManual ? (
                      <span className="rounded border border-amber-600 bg-amber-950/40 px-1.5 py-0.5 text-amber-300">
                        Manual edit
                      </span>
                    ) : (
                      <span className="rounded border border-cyan-700 bg-cyan-950/30 px-1.5 py-0.5 text-cyan-300">
                        AI-generated
                      </span>
                    )}
                    {it.feedback_action && it.feedback_action !== "edit" ? (
                      <span className="rounded bg-gray-700 px-1.5 py-0.5 text-gray-300">
                        {it.feedback_action}
                      </span>
                    ) : null}
                  </div>
                  <div className="flex items-center gap-2">
                    {it.created_at ? (
                      <span className="text-gray-500">
                        {new Date(it.created_at).toLocaleTimeString()}
                      </span>
                    ) : null}
                  </div>
                </div>

                <pre className="mb-2 max-h-32 overflow-auto whitespace-pre-wrap break-words rounded bg-gray-800 p-2 font-mono text-gray-300">
                  {it.generated_sql}
                </pre>

                <div className="flex flex-wrap gap-x-4 gap-y-1 text-gray-400">
                  {it.confidence != null ? (
                    <span>Confidence: {(it.confidence * 100).toFixed(0)}%</span>
                  ) : null}
                  {it.execution_rows != null ? <span>Rows: {it.execution_rows}</span> : null}
                  {it.execution_ms != null ? (
                    <span>Time: {it.execution_ms.toFixed(0)}ms</span>
                  ) : null}
                </div>

                {it.rationale ? (
                  <div className="mt-1 text-gray-500 italic">{it.rationale}</div>
                ) : null}

                {it.feedback_comment ? (
                  <div className="mt-1 rounded border border-gray-700 bg-gray-900 p-2 text-gray-300">
                    <span className="font-semibold text-gray-400">Feedback: </span>
                    {it.feedback_comment}
                  </div>
                ) : null}

                {it.error_message ? (
                  <div className="mt-1 rounded border border-red-800 bg-red-950/30 p-2 text-red-300">
                    <span className="font-semibold text-red-400">Error: </span>
                    {it.error_message}
                  </div>
                ) : null}
              </div>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const colors: Record<string, string> = {
    executed: "bg-emerald-600 text-white",
    approved: "bg-green-600 text-white",
    failed: "bg-red-600 text-white",
    pending: "bg-yellow-600 text-gray-950",
    superseded: "bg-gray-600 text-gray-300",
    validated: "bg-blue-600 text-white",
  };
  const color = colors[status] || "bg-gray-600 text-gray-300";
  return <span className={`rounded px-1.5 py-0.5 font-semibold ${color}`}>{status}</span>;
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
