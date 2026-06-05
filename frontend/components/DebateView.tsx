"use client";

import type { DebateTranscriptUI } from "@/components/NlSqlInterface";

export default function DebateView({ transcript }: { transcript: DebateTranscriptUI }) {
  return (
    <div className="space-y-3 rounded-md border border-indigo-700 bg-gray-900 p-4">
      <div className="text-sm font-semibold text-indigo-200">Debate Transcript</div>
      {transcript.summary && (
        <div className="grid grid-cols-2 gap-2 text-xs text-gray-300 md:grid-cols-4">
          <div>
            <span className="text-gray-500">Rounds</span>
            <div className="font-mono">{transcript.rounds?.length ?? String(transcript.summary.total_rounds ?? "-")}</div>
          </div>
          <div>
            <span className="text-gray-500">Termination</span>
            <div className="font-mono">{String(transcript.summary.termination_reason ?? "")}</div>
          </div>
          <div>
            <span className="text-gray-500">Critic Score</span>
            <div className="font-mono">{typeof transcript.summary.critic_score === "number" ? transcript.summary.critic_score.toFixed(2) : "-"}</div>
          </div>
          <div>
            <span className="text-gray-500">Confidence</span>
            <div className="font-mono">{typeof transcript.summary.final_confidence === "number" ? transcript.summary.final_confidence.toFixed(2) : "-"}</div>
          </div>
        </div>
      )}
      <div className="space-y-2">
        {(transcript.turns ?? []).map((turn: { round_number: number; speaker: string; timestamp: number; sql_candidate: string; query_hash: string; scores: Record<string, number>; objections: string[]; rationale: string; confidence: number | null; approved: boolean | null; token_usage: Record<string, number>; latency_ms: number; content: string }, idx: number) => (
          <div key={idx} className={`rounded border p-3 ${turn.speaker === "DebateAuthor" ? "border-indigo-700 bg-indigo-950/30" : "border-rose-700 bg-rose-950/30"}`}>
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs font-semibold text-gray-200">
                {turn.speaker}
              </span>
              <span className="text-xs text-gray-500">Round {turn.round_number}</span>
              {turn.confidence != null && (
                <span className="text-xs text-gray-400">Confidence: {(turn.confidence * 100).toFixed(0)}%</span>
              )}
              {typeof turn.latency_ms === "number" && (
                <span className="text-xs text-gray-500">{turn.latency_ms.toFixed(0)}ms</span>
              )}
              {turn.approved != null && (
                <span className={`rounded px-1.5 py-0.5 text-xs ${turn.approved ? "bg-green-600 text-white" : "bg-red-600 text-white"}`}>
                  {turn.approved ? "Approved" : "Rejected"}
                </span>
              )}
            </div>
            {turn.scores && Object.keys(turn.scores).length > 0 && (
              <div className="mt-2 grid grid-cols-2 gap-1 text-xs md:grid-cols-4">
                {Object.entries(turn.scores).map(([k, v]) => (
                  <div key={k} className="rounded bg-gray-800/60 p-1">
                    <span className="text-gray-500">{k}</span>
                    <div className="font-mono text-gray-200">{typeof v === "number" ? v.toFixed(2) : "-"}</div>
                  </div>
                ))}
              </div>
            )}
            {turn.objections.length > 0 && (
              <ul className="mt-2 list-disc space-y-1 pl-4 text-xs text-rose-200">
                {turn.objections.map((o: string, i: number) => (
                  <li key={i}>{o}</li>
                ))}
              </ul>
            )}
            {turn.sql_candidate && (
              <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap break-words rounded bg-gray-800 p-2 font-mono text-xs text-gray-200">
                {turn.sql_candidate}
              </pre>
            )}
            {turn.rationale && (
              <p className="mt-2 text-xs text-gray-300">{turn.rationale}</p>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
