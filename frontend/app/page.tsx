import HealthStatus from "@/components/HealthStatus";
import NlSqlInterface from "@/components/NlSqlInterface";
import SandboxDiagnostics from "@/components/SandboxDiagnostics";

export default function Home() {
  return (
    <main className="min-h-screen px-5 py-6 sm:px-8">
      <div className="mx-auto flex w-full max-w-6xl flex-col gap-6">
        <header className="flex flex-col gap-4 border-b border-gray-700 pb-5 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h1 className="text-3xl font-semibold text-white">NL SQL Agent</h1>
            <p className="mt-1 text-sm text-gray-400">AI-powered natural language to SQL</p>
          </div>
          <div className="rounded-md border border-gray-700 bg-gray-900 px-4 py-3">
            <HealthStatus />
          </div>
        </header>

        <NlSqlInterface />
        <details className="group">
          <summary className="cursor-pointer text-sm font-medium text-gray-400 hover:text-gray-200">
            Sandbox Diagnostics
          </summary>
          <div className="mt-4">
            <SandboxDiagnostics />
          </div>
        </details>
      </div>
    </main>
  );
}
