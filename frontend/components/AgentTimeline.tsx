"use client";

import { useMemo, useState } from "react";
import type { AgentEvent } from "@/lib/useAgentStream";

interface Props {
  events: AgentEvent[];
  connected: boolean;
  reconnecting: boolean;
  isPolling: boolean;
  visible: boolean;
}

const AGENT_LABELS: Record<string, string> = {
  intent_analyst: "Analyzing Intent",
  schema_designer: "Loading Schema",
  query_author: "Generating SQL",
  critic: "Validating SQL",
  debate: "Debate (if enabled)",
  debateauthor: "Debate Author",
  debatecritic: "Debate Critic",
  polling: "Status Update",
};

const AGENT_COLORS: Record<string, string> = {
  intent_analyst: "bg-blue-500",
  schema_designer: "bg-purple-500",
  query_author: "bg-amber-500",
  critic: "bg-rose-500",
  debate: "bg-indigo-500",
  debateauthor: "bg-indigo-400",
  debatecritic: "bg-rose-400",
  polling: "bg-gray-500",
};

const AGENT_BORDER_COLORS: Record<string, string> = {
  intent_analyst: "border-blue-500",
  schema_designer: "border-purple-500",
  query_author: "border-amber-500",
  critic: "border-rose-500",
  debate: "border-indigo-500",
  debateauthor: "border-indigo-400",
  debatecritic: "border-rose-400",
  polling: "border-gray-500",
};

interface AgentPhase {
  agent: string;
  status: string;
  progress: number;
  partialText: string | null;
  artifact: Record<string, unknown> | null;
  isActive: boolean;
  isComplete: boolean;
  isError: boolean;
  isWarning: boolean;
}

function buildAgentPhases(events: AgentEvent[]): AgentPhase[] {
  const agentOrder = [
    "connection",
    "intent_analyst",
    "schema_designer",
    "query_author",
    "critic",
    "debate",
    "debateauthor",
    "debatecritic",
  ];
  const phases: Record<string, AgentPhase> = {};

  for (const agent of agentOrder) {
    const agentEvents = events.filter((e) => e.agent === agent);
    const lastEvent = agentEvents[agentEvents.length - 1];
    const startEvent = agentEvents.find((e) => e.phase === "start");
    const hasError = agentEvents.some((e) => e.phase === "error");
    const hasWarning = agentEvents.some((e) => e.phase === "warning");
    const isComplete = agentEvents.some((e) => e.phase === "complete");
    const progressEvents = agentEvents.filter(
      (e) => e.progress_percent != null,
    );
    const maxProgress =
      progressEvents.length > 0
        ? Math.max(...progressEvents.map((e) => e.progress_percent!))
        : 0;

    if (startEvent || lastEvent) {
      phases[agent] = {
        agent,
        status: lastEvent?.status || AGENT_LABELS[agent] || agent,
        progress: isComplete ? 100 : maxProgress,
        partialText: lastEvent?.partial_text || null,
        artifact: lastEvent?.artifact || null,
        isActive: !!startEvent && !isComplete && !hasError,
        isComplete,
        isError: hasError,
        isWarning: hasWarning,
      };
    } else {
      phases[agent] = {
        agent,
        status: AGENT_LABELS[agent] || agent,
        progress: 0,
        partialText: null,
        artifact: null,
        isActive: false,
        isComplete: false,
        isError: false,
        isWarning: false,
      };
    }
  }

  return agentOrder.filter((a) => phases[a]).map((a) => phases[a]);
}

function ProgressBar({
  progress,
  isActive,
  isComplete,
  isError,
  color,
}: {
  progress: number;
  isActive: boolean;
  isComplete: boolean;
  isError: boolean;
  color: string;
}) {
  const width = Math.max(progress, isActive ? 5 : 0);
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-gray-700">
      <div
        className={`h-full rounded-full transition-all duration-500 ease-out ${isComplete ? "bg-green-500" : isError ? "bg-red-500" : color} ${isActive && !isComplete ? "animate-pulse" : ""}`}
        style={{ width: `${width}%` }}
      />
    </div>
  );
}

function ExpandableSection({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="mt-1">
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-1 text-xs text-gray-400 hover:text-gray-200 transition-colors"
      >
        <span
          className={`text-xs transition-transform ${open ? "rotate-90" : ""}`}
        >
          &#9654;
        </span>
        {label}
      </button>
      {open && (
        <div className="mt-1 rounded border border-gray-600 bg-gray-800 p-2 text-xs text-gray-300 overflow-auto max-h-40">
          {children}
        </div>
      )}
    </div>
  );
}

function AgentCard({ phase }: { phase: AgentPhase }) {
  const label = AGENT_LABELS[phase.agent] || phase.agent;
  const color = AGENT_COLORS[phase.agent] || "bg-gray-500";
  const borderColor = AGENT_BORDER_COLORS[phase.agent] || "border-gray-500";
  const statusIcon = phase.isComplete
    ? "\u2705"
    : phase.isError
      ? "\u274C"
      : phase.isWarning
        ? "\u26A0\uFE0F"
        : phase.isActive
          ? "\u23F3"
          : "\u23ED";

  return (
    <div
      className={`rounded border ${phase.isActive ? `${borderColor} ring-1 ring-opacity-30` : "border-gray-700"} bg-gray-900 p-3 transition-all`}
    >
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <span className="flex-shrink-0 text-sm">{statusIcon}</span>
          <span className="text-sm font-medium text-white truncate">
            {label}
          </span>
          {phase.isActive && (
            <span className="flex-shrink-0 flex gap-1">
              <span
                className="h-1.5 w-1.5 animate-bounce rounded-full bg-current"
                style={{ animationDelay: "0ms" }}
              />
              <span
                className="h-1.5 w-1.5 animate-bounce rounded-full bg-current"
                style={{ animationDelay: "150ms" }}
              />
              <span
                className="h-1.5 w-1.5 animate-bounce rounded-full bg-current"
                style={{ animationDelay: "300ms" }}
              />
            </span>
          )}
        </div>
        <span
          className={`flex-shrink-0 text-xs ${phase.isError ? "text-red-400" : phase.isComplete ? "text-green-400" : "text-gray-400"}`}
        >
          {phase.isComplete
            ? "Done"
            : phase.isError
              ? "Failed"
              : phase.isActive
                ? `${phase.progress}%`
                : "Waiting"}
        </span>
      </div>

      <ProgressBar
        progress={phase.progress}
        isActive={phase.isActive}
        isComplete={phase.isComplete}
        isError={phase.isError}
        color={color}
      />

      {phase.status &&
        (phase.isActive || phase.isComplete || phase.isError) && (
          <p
            className={`mt-1 text-xs ${phase.isError ? "text-red-400" : "text-gray-400"}`}
          >
            {phase.status}
          </p>
        )}

      {phase.isComplete && phase.artifact && (
        <ExpandableSection label="Show details">
          <pre className="whitespace-pre-wrap break-all">
            {JSON.stringify(phase.artifact, null, 2)}
          </pre>
        </ExpandableSection>
      )}

      {phase.isError && phase.partialText && (
        <div className="mt-1 rounded border border-red-800 bg-red-950/50 p-1.5 text-xs text-red-300 overflow-auto max-h-20">
          {phase.partialText}
        </div>
      )}

      {phase.isWarning && phase.partialText && (
        <div className="mt-1 rounded border border-amber-800 bg-amber-950/50 p-1.5 text-xs text-amber-300 overflow-auto max-h-20">
          {phase.partialText}
        </div>
      )}

      {phase.isActive && phase.partialText && (
        <ExpandableSection label="Show intermediate output">
          <pre className="whitespace-pre-wrap break-all">
            {phase.partialText}
          </pre>
        </ExpandableSection>
      )}
    </div>
  );
}

export default function AgentTimeline({
  events,
  connected,
  reconnecting,
  isPolling,
  visible,
}: Props) {
  const phases = useMemo(() => buildAgentPhases(events), [events]);
  const [collapsed, setCollapsed] = useState(false);

  if (!visible) return null;

  return (
    <div className="w-full max-w-5xl space-y-2">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <h3 className="text-sm font-semibold text-gray-300">
            Agent Activity
          </h3>
          <span
            className={`inline-block h-2 w-2 rounded-full ${connected ? "bg-green-500" : reconnecting ? "bg-yellow-500 animate-pulse" : "bg-yellow-500 animate-pulse"}`}
          />
          <span className="text-xs text-gray-500">
            {connected
              ? "Live"
              : reconnecting
                ? "Reconnecting..."
                : isPolling
                  ? "Polling"
                  : "Connecting..."}
          </span>
        </div>
        <button
          onClick={() => setCollapsed(!collapsed)}
          className="text-xs text-gray-500 hover:text-gray-300 transition-colors"
        >
          {collapsed ? "Show timeline" : "Hide timeline"}
        </button>
      </div>

      {!collapsed && (
        <div className="space-y-1.5">
          {phases.map((phase) => (
            <AgentCard key={phase.agent} phase={phase} />
          ))}
        </div>
      )}
    </div>
  );
}
