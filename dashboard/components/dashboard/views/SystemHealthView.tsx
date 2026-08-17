"use client";

import { useFactoryStream } from "@/hooks/use-factory-stream";
import { Activity, Server, Cpu, ShieldAlert } from "lucide-react";
import { motion } from "motion/react";
import { SystemFlowVisualizer } from "./SystemFlowVisualizer";

export function SystemHealthView() {
  const { health, engines, activity } = useFactoryStream();

  if (!health) {
    return <div className="text-sm text-slate-500">Loading system health...</div>;
  }

  const activeEnginesCount = engines.filter((e) => e.status === "running").length;
  const recentActivity = activity.slice(0, 8);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold tracking-tight text-slate-900">System Health</h2>
          <p className="text-sm text-slate-500 mt-1">Global factory state and engine capacity</p>
        </div>
        <div className="flex items-center gap-2 text-sm text-slate-500 font-mono">
          <span
            className={`flex h-2 w-2 rounded-full ${health.mode === "active" ? "bg-emerald-500 animate-pulse" : "bg-amber-500"}`}
          ></span>
          {health.mode.toUpperCase()} MODE
        </div>
      </div>

      {/* Global Safety State */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
        <HealthCard
          title="Database State"
          status={health.db_connected ? "OK" : "DOWN"}
          icon={Server}
          warning={!health.db_connected}
        />
        <HealthCard
          title="Sandbox"
          status={health.sandbox_health}
          icon={Activity}
          warning={health.sandbox_health !== "clean"}
        />
        <HealthCard
          title="Global Pause"
          status={health.global_pause ? "TRUE" : "FALSE"}
          icon={ShieldAlert}
          warning={health.global_pause}
        />
        <HealthCard title="Active Engines" status={`${activeEnginesCount} / ${engines.length}`} icon={Cpu} />
      </div>

      {/* Provider health -- declared in the original mock's types but never
          actually rendered anywhere; added here since it's real data now. */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {Object.entries(health.provider_health).map(([provider, info]) => (
          <div
            key={provider}
            className={`bg-white p-4 rounded-xl border shadow-sm flex items-center justify-between ${
              info.status === "down"
                ? "border-rose-300 bg-rose-50"
                : info.status === "degraded"
                  ? "border-amber-300 bg-amber-50"
                  : "border-slate-200"
            }`}
          >
            <div>
              <h3 className="text-xs font-bold tracking-wider uppercase text-slate-500 capitalize">
                {provider}
              </h3>
              <div className="text-lg font-mono font-bold text-slate-900 uppercase">{info.status}</div>
            </div>
            <div className="text-xs text-slate-500 font-mono text-right">
              <div>{info.errors_30m} errors</div>
              <div>{info.calls_30m} calls / 30m</div>
            </div>
          </div>
        ))}
      </div>

      <SystemFlowVisualizer />

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Engine Capacity */}
        <div className="lg:col-span-2 bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden flex flex-col h-[400px]">
          <div className="p-4 border-b border-slate-200 bg-slate-50 flex justify-between items-center">
            <h3 className="font-semibold text-slate-800">Engine Capacity & Throttling</h3>
          </div>
          <div className="p-0 overflow-y-auto flex-1">
            <table className="w-full text-sm text-left">
              <thead className="bg-slate-50 text-slate-500 font-mono text-xs uppercase border-b border-slate-200 sticky top-0">
                <tr>
                  <th className="px-4 py-3">Engine</th>
                  <th className="px-4 py-3">Status</th>
                  <th className="px-4 py-3">In Flight</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {engines.map((engine) => (
                  <tr key={engine.name} className="hover:bg-slate-50">
                    <td className="px-4 py-3 font-mono text-xs font-bold text-slate-700 capitalize">
                      {engine.name}
                    </td>
                    <td className="px-4 py-3">
                      <span
                        className={`inline-flex items-center rounded-md px-2 py-1 text-xs font-medium ring-1 ring-inset ${
                          engine.status === "running"
                            ? "bg-emerald-50 text-emerald-700 ring-emerald-500/20"
                            : engine.status === "paused"
                              ? "bg-rose-50 text-rose-700 ring-rose-500/20"
                              : "bg-slate-100 text-slate-700 ring-slate-500/20"
                        }`}
                        title={engine.disabled_reason ?? undefined}
                      >
                        {engine.status}
                      </span>
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-2">
                        <span className="font-mono text-xs text-slate-500">
                          {engine.in_flight}
                          {engine.max_concurrency !== null ? ` / ${engine.max_concurrency}` : ""}
                        </span>
                        {engine.max_concurrency !== null && (
                          <div className="w-16 h-1.5 bg-slate-100 rounded-full overflow-hidden">
                            <motion.div
                              className={`h-full rounded-full ${
                                engine.in_flight >= engine.max_concurrency ? "bg-rose-500" : "bg-blue-500"
                              }`}
                              animate={{
                                width: `${Math.min(100, (engine.in_flight / engine.max_concurrency) * 100)}%`,
                              }}
                            />
                          </div>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        {/* Recent Activity -- synthesized from real rows (tasks, interrupts),
            not a real log tail: no log table exists anywhere in the backend. */}
        <div className="bg-slate-950 rounded-xl border border-slate-800 shadow-sm overflow-hidden flex flex-col h-[400px]">
          <div className="p-4 border-b border-slate-800 flex justify-between items-center">
            <h3 className="font-semibold text-white flex items-center gap-2">
              <span className="w-2 h-2 rounded-full bg-emerald-500 animate-pulse" />
              Recent Activity
            </h3>
          </div>
          <div className="p-4 overflow-y-auto flex-1 font-mono text-xs space-y-2">
            {recentActivity.map((item) => (
              <motion.div
                key={item.id}
                initial={{ opacity: 0, x: -10 }}
                animate={{ opacity: 1, x: 0 }}
                className="flex items-start gap-2"
              >
                <span className="text-slate-500 shrink-0" suppressHydrationWarning>
                  [{new Date(item.created_at).toISOString().split("T")[1].slice(0, -1)}]
                </span>
                <span className={item.level === "warn" ? "text-amber-400" : "text-blue-400"}>
                  {item.source}:
                </span>
                <span className="text-slate-300 break-words">{item.message}</span>
              </motion.div>
            ))}
            {recentActivity.length === 0 && <div className="text-slate-600">No recent activity.</div>}
          </div>
        </div>
      </div>
    </div>
  );
}

function HealthCard({
  title,
  status,
  icon: Icon,
  warning = false,
}: {
  title: string;
  status: string;
  icon: React.ComponentType<{ className?: string }>;
  warning?: boolean;
}) {
  return (
    <div className={`bg-white p-4 rounded-xl border shadow-sm ${warning ? "border-rose-300 bg-rose-50" : "border-slate-200"}`}>
      <div className="flex items-center justify-between mb-2">
        <h3 className="text-xs font-bold tracking-wider uppercase text-slate-500">{title}</h3>
        <Icon className={`w-4 h-4 ${warning ? "text-rose-500" : "text-slate-400"}`} />
      </div>
      <div className={`text-lg font-mono font-bold ${warning ? "text-rose-700" : "text-slate-900"}`}>{status}</div>
    </div>
  );
}
