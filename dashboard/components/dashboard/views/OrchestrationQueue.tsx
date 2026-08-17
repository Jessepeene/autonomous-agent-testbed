import { useFactoryStream } from "@/hooks/use-factory-stream";
import type { TaskState } from "@/hooks/use-factory-stream";
import { ListTodo, Activity, AlertTriangle } from "lucide-react";
import { motion } from "motion/react";

export function OrchestrationQueue() {
  const { tasks, metrics, state } = useFactoryStream();

  if (!metrics) {
    return <div className="text-sm text-slate-500">Loading task orchestration...</div>;
  }

  const activeTasks = tasks.filter((t) => t.status === "running" || t.status === "claimed");
  const pendingCount = metrics.tasks_by_status.pending ?? 0;
  const escalatedCount = metrics.tasks_by_status.escalated ?? 0;
  const maxAttempts = state?.max_task_attempts ?? null;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold tracking-tight text-slate-900">Task Orchestration</h2>
          <p className="text-sm text-slate-500 mt-1">Live queue pressure, task leasing, and escalations</p>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div className="bg-white p-5 rounded-xl border border-slate-200 shadow-sm">
          <div className="flex items-center justify-between mb-2">
            <h3 className="text-sm font-medium text-slate-500">Queue Pressure</h3>
            <ListTodo className="w-4 h-4 text-slate-400" />
          </div>
          <div className="text-2xl font-bold text-slate-900">{pendingCount}</div>
          <div className="text-xs text-slate-500 mt-1">Pending tasks waiting</div>
        </div>
        <div className="bg-white p-5 rounded-xl border border-slate-200 shadow-sm">
          <div className="flex items-center justify-between mb-2">
            <h3 className="text-sm font-medium text-slate-500">Active Leases</h3>
            <Activity className="w-4 h-4 text-slate-400" />
          </div>
          <div className="text-2xl font-bold text-slate-900">{activeTasks.length}</div>
          <div className="text-xs text-slate-500 mt-1">Tasks currently locked by engines</div>
        </div>
        <div className={`p-5 rounded-xl border shadow-sm ${escalatedCount > 0 ? "bg-rose-50 border-rose-300" : "bg-white border-slate-200"}`}>
          <div className="flex items-center justify-between mb-2">
            <h3 className="text-sm font-medium text-slate-500">Escalated</h3>
            <AlertTriangle className={`w-4 h-4 ${escalatedCount > 0 ? "text-rose-500" : "text-slate-400"}`} />
          </div>
          <div className={`text-2xl font-bold ${escalatedCount > 0 ? "text-rose-700" : "text-slate-900"}`}>{escalatedCount}</div>
          <div className="text-xs text-slate-500 mt-1">Exhausted retries, needs HITL</div>
        </div>
      </div>

      <div className="bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden flex flex-col h-[500px]">
        <div className="p-4 border-b border-slate-200 bg-slate-50 flex justify-between items-center">
          <h3 className="font-semibold text-slate-800">Orchestrator Task Stream</h3>
        </div>
        <div className="p-0 overflow-y-auto flex-1">
          <table className="w-full text-sm text-left">
            <thead className="bg-slate-50 text-slate-500 font-mono text-xs uppercase border-b border-slate-200 sticky top-0 z-10">
              <tr>
                <th className="px-4 py-3">Task ID / Kind</th>
                <th className="px-4 py-3">Priority</th>
                <th className="px-4 py-3">Idempotency Key</th>
                <th className="px-4 py-3">Engine Lease</th>
                <th className="px-4 py-3">Attempts</th>
                <th className="px-4 py-3">State</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {tasks.map((task) => (
                <motion.tr
                  key={task.id}
                  initial={{ opacity: 0, backgroundColor: "#f8fafc" }}
                  animate={{ opacity: 1, backgroundColor: "#ffffff" }}
                  className="hover:bg-slate-50"
                >
                  <td className="px-4 py-3">
                    <div className="font-mono text-xs text-slate-500">{task.id}</div>
                    <div className="font-medium text-slate-900">{task.kind}</div>
                  </td>
                  <td className="px-4 py-3">
                    <PriorityBadge priority={task.priority} />
                  </td>
                  <td className="px-4 py-3 font-mono text-xs text-slate-400">
                    {task.idempotency_key}
                  </td>
                  <td className="px-4 py-3">
                    <span className="inline-flex items-center rounded-md bg-slate-100 px-2 py-1 text-xs font-medium text-slate-600 ring-1 ring-inset ring-slate-500/10">
                      {task.engine || "UNASSIGNED"}
                    </span>
                  </td>
                  <td className="px-4 py-3 font-mono text-xs text-slate-500">
                    <span className={task.attempt_count > 0 ? "text-amber-600 font-bold" : ""}>{task.attempt_count}</span>
                    {maxAttempts !== null ? ` / ${maxAttempts}` : ""}
                  </td>
                  <td className="px-4 py-3">
                    <StatusBadge status={task.status} />
                  </td>
                </motion.tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

// Raw int, lower = more urgent (10 = finance, 100 = default, 500 = backlog) --
// there's no priority enum in the backend, this banding is presentation-only.
function PriorityBadge({ priority }: { priority: number }) {
  const style =
    priority <= 50
      ? "bg-rose-100 text-rose-800"
      : priority <= 150
        ? "bg-slate-100 text-slate-600"
        : "bg-slate-50 text-slate-500";
  return (
    <span className={`inline-flex items-center rounded-md px-2 py-0.5 text-[10px] font-bold font-mono ${style}`}>
      P{priority}
    </span>
  );
}

function StatusBadge({ status }: { status: TaskState }) {
  const colors: Record<TaskState, string> = {
    pending: "bg-slate-100 text-slate-700 ring-slate-500/20",
    claimed: "bg-blue-50 text-blue-700 ring-blue-500/20",
    running: "bg-amber-50 text-amber-700 ring-amber-500/20",
    done: "bg-emerald-50 text-emerald-700 ring-emerald-500/20",
    failed: "bg-orange-50 text-orange-700 ring-orange-500/20 font-bold",
    escalated: "bg-rose-100 text-rose-800 ring-rose-500/30 font-bold animate-pulse",
  };

  return (
    <span className={`inline-flex items-center rounded-md px-2 py-1 text-[10px] font-bold ring-1 ring-inset uppercase ${colors[status]}`}>
      {status}
    </span>
  );
}
