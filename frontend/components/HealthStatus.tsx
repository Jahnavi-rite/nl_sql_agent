/**
 * HealthStatus — Client component that displays backend status.
 *
 * This is a "Client Component" (marked with "use client") because it:
 * - Uses React hooks (useState, useEffect)
 * - Runs in the browser
 * - Fetches data after the page loads
 *
 * Server Components (default) run on the server and can't use hooks.
 */

"use client";

import { useEffect, useState } from "react";
import { fetchHealth, type HealthResponse } from "@/lib/api";

export default function HealthStatus() {
  // State: holds the health data or error message
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  // Fetch health status when the component mounts
  useEffect(() => {
    let cancelled = false;

    async function load() {
      try {
        const data = await fetchHealth();
        if (!cancelled) {
          setHealth(data);
          setError(null);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Unknown error");
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    }

    load();

    // Cleanup: prevent state updates if component unmounts
    return () => {
      cancelled = true;
    };
  }, []);

  // Render loading state
  if (loading) {
    return (
      <div className="flex items-center gap-2 text-yellow-400">
        <div className="h-2 w-2 animate-pulse rounded-full bg-yellow-400" />
        <span>Checking backend...</span>
      </div>
    );
  }

  // Render error state
  if (error) {
    return (
      <div className="flex items-center gap-2 text-red-400">
        <div className="h-2 w-2 rounded-full bg-red-400" />
        <span>Backend status: error — {error}</span>
      </div>
    );
  }

  // Render success state
  return (
    <div className="flex items-center gap-2 text-green-400">
      <div className="h-2 w-2 rounded-full bg-green-400" />
      <span>
        Backend status: <strong>{health?.status}</strong>
        <span className="ml-2 text-sm text-gray-400">
          (v{health?.version})
        </span>
      </span>
    </div>
  );
}
