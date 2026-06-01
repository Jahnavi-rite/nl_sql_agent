"use client";

import { useState } from "react";

import {
  uploadDataset,
  type DatasetUploadResponse,
  type SandboxDialect,
} from "@/lib/api";

interface DatasetUploadProps {
  sessionId: string;
  dialect: SandboxDialect;
  getSessionId: () => Promise<string>;
  onDatasetUploaded: (dataset: DatasetUploadResponse) => void | Promise<void>;
}

export default function DatasetUpload({
  sessionId,
  dialect,
  getSessionId,
  onDatasetUploaded,
}: DatasetUploadProps) {
  const [file, setFile] = useState<File | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isUploading, setIsUploading] = useState(false);

  async function handleUpload() {
    if (!file) return;

    setError(null);
    setIsUploading(true);

    try {
      const sid = sessionId || await getSessionId();
      const result = await uploadDataset(sid, file, dialect);
      await onDatasetUploaded(result);
      setFile(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setIsUploading(false);
    }
  }

  return (
    <div className="rounded-md border border-gray-700 bg-gray-900 p-4">
      <h3 className="text-sm font-semibold text-gray-200">Upload Dataset</h3>
      <p className="mt-1 text-xs text-gray-500">
        Upload a CSV or Excel file to use as query data.
      </p>

      <div className="mt-3 flex items-center gap-3">
        <input
          accept=".csv,.xls,.xlsx"
          className="block w-full text-xs text-gray-400 file:mr-3 file:rounded file:border-0 file:bg-gray-800 file:px-3 file:py-1.5 file:text-xs file:font-medium file:text-gray-200 hover:file:bg-gray-700"
          disabled={isUploading}
          id="dataset-file"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          type="file"
        />
        <button
          className="whitespace-nowrap rounded-md bg-cyan-500 px-4 py-2 text-xs font-semibold text-gray-950 transition hover:bg-cyan-400 disabled:cursor-not-allowed disabled:bg-gray-700 disabled:text-gray-400"
          disabled={isUploading || !file}
          onClick={handleUpload}
          type="button"
        >
          {isUploading ? "Uploading..." : "Upload"}
        </button>
      </div>

      {error ? (
        <div className="mt-2 rounded border border-red-800 bg-red-950/40 p-2 text-xs text-red-200">
          {error}
        </div>
      ) : null}
    </div>
  );
}
