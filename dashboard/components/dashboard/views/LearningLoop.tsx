import { useFactoryStream } from "@/hooks/use-factory-stream";
import type { Trial, TrialStatus } from "@/hooks/use-factory-stream";
import { BrainCircuit, Activity, CheckCircle2, Archive } from "lucide-react";
import type { LucideIcon } from "lucide-react";

function rate(numerator: number, denominator: number): string {
  return denominator > 0 ? `${((numerator / denominator) * 100).toFixed(1)}%` : "—";
}

export function LearningLoop() {
  const { trials } = useFactoryStream();

  return (
    <div className="space-y-6">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div>
          <h2 className="text-2xl font-bold tracking-tight text-slate-900">Learning Trials</h2>
          <p className="text-sm text-slate-500 mt-1">Statistical evaluation of prompt and parameter mutations</p>
        </div>
      </div>

      <div className="bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm text-left">
            <thead className="bg-slate-50 text-slate-600 font-mono text-xs uppercase border-b border-slate-200">
              <tr>
                <th className="px-6 py-4">Key</th>
                <th className="px-6 py-4">Target Metric</th>
                <th className="px-6 py-4">State</th>
                <th className="px-6 py-4">Baseline vs Trial</th>
                <th className="px-6 py-4">Significance</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {trials.map((trial) => {
                const baselineRate =
                  trial.baseline_numerator !== null && trial.baseline_denominator !== null
                    ? rate(trial.baseline_numerator, trial.baseline_denominator)
                    : null;
                const trialRate = rate(trial.numerator, trial.denominator);
                return (
                  <tr key={trial.id} className="hover:bg-slate-50 transition-colors">
                    <td className="px-6 py-4 font-medium text-slate-900 font-mono text-xs">
                      {trial.key}
                      <div className="text-slate-400 font-normal">{trial.kind}</div>
                    </td>
                    <td className="px-6 py-4 font-mono text-xs text-slate-500">{trial.target_metric}</td>
                    <td className="px-6 py-4">
                      <TrialBadge status={trial.status} />
                    </td>
                    <td className="px-6 py-4">
                      <div className="flex items-center gap-2 font-mono">
                        <span className="text-slate-500">{baselineRate ?? "no baseline"}</span>
                        {baselineRate !== null && <span className="text-slate-400">→</span>}
                        <span className="font-bold text-slate-800">
                          {trial.denominator > 0 ? trialRate : "pending"}
                        </span>
                        <span className="text-slate-400 text-xs">
                          ({trial.numerator}/{trial.denominator})
                        </span>
                      </div>
                    </td>
                    <td className="px-6 py-4">
                      {trial.p_value !== null ? (
                        <span
                          className={`font-mono text-xs font-bold ${trial.p_value < 0.05 ? "text-emerald-600" : "text-slate-500"}`}
                          title="Fisher's exact test, one-sided"
                        >
                          p = {trial.p_value.toFixed(4)}
                          {trial.p_value < 0.05 && " (significant)"}
                        </span>
                      ) : (
                        <span className="text-slate-400 italic text-xs">Evaluating...</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      <div className="p-5 bg-indigo-50 border border-indigo-100 rounded-xl flex items-start gap-4">
        <div className="mt-1">
          <BrainCircuit className="w-6 h-6 text-indigo-600" />
        </div>
        <div>
          <h4 className="text-sm font-bold text-indigo-900 mb-1">Guarded Mutations</h4>
          <p className="text-sm text-indigo-800 leading-relaxed max-w-3xl">
            The orchestrator cannot arbitrarily mutate logic. Candidates run side-by-side with baselines, evaluated
            with a one-sided Fisher&apos;s exact test against real outcomes before being promoted or retired.
          </p>
        </div>
      </div>
    </div>
  );
}

function TrialBadge({ status }: { status: TrialStatus }) {
  const config: Record<TrialStatus, { color: string; icon: LucideIcon | null }> = {
    candidate: { color: "bg-slate-100 text-slate-700 ring-slate-500/20", icon: null },
    trial: { color: "bg-blue-50 text-blue-700 ring-blue-500/20", icon: Activity },
    active: { color: "bg-emerald-50 text-emerald-700 ring-emerald-500/20", icon: CheckCircle2 },
    retired: { color: "bg-slate-50 text-slate-500 ring-slate-500/20", icon: Archive },
  };

  const { color, icon: Icon } = config[status];

  return (
    <span className={`inline-flex w-fit items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-medium ring-1 ring-inset ${color}`}>
      {Icon && <Icon className="w-3 h-3" />}
      {status}
    </span>
  );
}
