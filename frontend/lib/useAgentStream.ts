"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { getWsUrl } from "@/lib/api";

export interface AgentEvent {
  type: "event" | "ping" | "pong" | "reconnect";
  agent: string;
  phase: string;
  timestamp: number;
  partial_text: string | null;
  artifact: Record<string, unknown> | null;
  progress_percent: number | null;
  status: string;
  request_id: string;
}

const RECONNECT_DELAYS = [500, 1000, 2000, 4000, 8000];
const POLL_INTERVAL_MS = 3000;

export function useAgentStream(sessionId: string | null, requestId: string | null) {
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const [reconnecting, setReconnecting] = useState(false);

  const wsRef = useRef<WebSocket | null>(null);
  const reconnectCountRef = useRef(0);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pollTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const isPollingRef = useRef(false);
  const mountedRef = useRef(true);

  const clearTimers = useCallback(() => {
    if (reconnectTimerRef.current) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
    if (pollTimerRef.current) {
      clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  }, []);

  const connect = useCallback(() => {
    if (!sessionId || !mountedRef.current) return;

    if (wsRef.current) {
      wsRef.current.onclose = null;
      wsRef.current.onerror = null;
      wsRef.current.onmessage = null;
      if (wsRef.current.readyState === WebSocket.OPEN || wsRef.current.readyState === WebSocket.CONNECTING) {
        wsRef.current.close();
      }
    }

    const wsUrl = `${getWsUrl()}/sessions/${sessionId}/stream`;
    const isReconnect = reconnectCountRef.current > 0;

    try {
      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;

      ws.onopen = () => {
        if (!mountedRef.current) { ws.close(); return; }
        setConnected(true);
        setReconnecting(false);
        reconnectCountRef.current = 0;
        if (isPollingRef.current) {
          isPollingRef.current = false;
          if (pollTimerRef.current) {
            clearInterval(pollTimerRef.current);
            pollTimerRef.current = null;
          }
        }
      };

      ws.onmessage = (msg: MessageEvent) => {
        if (!mountedRef.current) return;
        try {
          const data = JSON.parse(msg.data);
          if (data.type === "pong") return;
          if (data.type === "ping") {
            ws.send(JSON.stringify({ type: "pong" }));
            return;
          }
          if (data.type === "event") {
            setEvents((prev) => [...prev, data as AgentEvent]);
          }
        } catch {
          // ignore parse errors
        }
      };

      ws.onclose = () => {
        if (!mountedRef.current) return;
        setConnected(false);
        if (isReconnect) {
          startReconnect();
        } else {
          startPolling();
        }
      };

      ws.onerror = () => {
        if (!mountedRef.current) return;
        ws.close();
      };
    } catch {
      startPolling();
    }
  }, [sessionId]);

  const startReconnect = useCallback(() => {
    if (!mountedRef.current || !sessionId) return;
    setReconnecting(true);
    const delay = RECONNECT_DELAYS[Math.min(reconnectCountRef.current, RECONNECT_DELAYS.length - 1)];
    reconnectCountRef.current++;
    reconnectTimerRef.current = setTimeout(() => {
      if (mountedRef.current) connect();
    }, delay);
  }, [sessionId, connect]);

  const startPolling = useCallback(() => {
    if (!mountedRef.current || !sessionId || !requestId || isPollingRef.current) return;
    isPollingRef.current = true;
    pollTimerRef.current = setInterval(async () => {
      if (!mountedRef.current || !sessionId || !requestId) return;
      try {
        const resp = await fetch(`${getWsUrl().replace("ws://", "http://")}/sessions/${sessionId}/requests/${requestId}`);
        if (resp.ok) {
          const data = await resp.json();
          if (data.iterations && data.iterations.length > 0) {
            setEvents((prev) => {
              const hasCompletion = prev.some((e) => e.phase === "complete" && e.agent === "critic");
              if (hasCompletion) return prev;
              return [
                ...prev,
                {
                  type: "event",
                  agent: "polling",
                  phase: "progress",
                  timestamp: Date.now() / 1000,
                  partial_text: `Polled status: ${data.request_status || data.status}`,
                  artifact: null,
                  progress_percent: null,
                  status: `Status: ${data.request_status || data.status}`,
                  request_id: requestId,
                },
              ];
            });
            if (data.request_status === "approved" || data.status === "completed") {
              setEvents((prev) => [
                ...prev,
                {
                  type: "event",
                  agent: "critic",
                  phase: "complete",
                  timestamp: Date.now() / 1000,
                  partial_text: null,
                  artifact: null,
                  progress_percent: 100,
                  status: "Pipeline complete",
                  request_id: requestId,
                },
              ]);
              if (pollTimerRef.current) clearInterval(pollTimerRef.current);
              pollTimerRef.current = null;
              isPollingRef.current = false;
            }
          }
        }
      } catch {
        // ignore poll errors
      }
    }, POLL_INTERVAL_MS);
  }, [sessionId, requestId]);

  const disconnect = useCallback(() => {
    mountedRef.current = false;
    clearTimers();
    if (wsRef.current) {
      wsRef.current.onclose = null;
      wsRef.current.close();
      wsRef.current = null;
    }
  }, [clearTimers]);

  useEffect(() => {
    mountedRef.current = true;
    if (sessionId) {
      connect();
    }
    return () => {
      disconnect();
    };
  }, [sessionId, connect, disconnect]);

  useEffect(() => {
    if (requestId) {
      setEvents([]);
    }
  }, [requestId]);

  const clearEvents = useCallback(() => {
    setEvents([]);
  }, []);

  return {
    events,
    connected,
    reconnecting,
    clearEvents,
    isPolling: isPollingRef.current,
  };
}
