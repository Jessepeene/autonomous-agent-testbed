import { useFactoryStream } from "@/hooks/use-factory-stream";
import { DollarSign, ShieldCheck, TrendingUp, AlertTriangle, Wallet, Clock } from "lucide-react";
import type { LucideIcon } from "lucide-react";

function timeAgo(iso: string): string {
  const min = Math.floor((Date.now() - new Date(iso).getTime()) / 60000);
  if (min < 1) return "just now";
  if (min < 60) return `${min}m ago`;
  const hrs = Math.floor(min / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

export function FinanceSpend() {
  const { metrics, health, state } = useFactoryStream();

  if (!metrics || !health) {
    return <div className="text-sm text-slate-500">Loading finance & spend...</div>;
  }

  const cap = health.daily_llm_spend_cap_usd;
  const isBudgetExceeded = health.global_pause;

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-bold tracking-tight text-slate-900">Finance & Spend</h2>
        <p className="text-sm text-slate-500 mt-1">Financial ledger, API burn rate, and budget circuit breakers</p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <MetricCard
          title="Total Live MRR"
          value={`$${(metrics.mrr_cents / 100).toLocaleString()}`}
          icon={TrendingUp}
          color="emerald"
        />
        <MetricCard
          title="Daily LLM Spend"
          value={`$${metrics.llm_spend_usd_24h.toFixed(2)}`}
          subtitle={cap !== null ? `Cap: $${cap.toFixed(2)}` : "No cap configured"}
          icon={DollarSign}
          color={isBudgetExceeded ? "rose" : "slate"}
        />
      </div>

      <div className="bg-slate-900 rounded-xl p-5 border border-slate-800 text-white shadow-sm">
        <h3 className="text-sm font-bold flex items-center gap-2 mb-4">
          <ShieldCheck className="w-5 h-5 text-emerald-400" />
          Circuit Breakers & Ledger State
        </h3>

        <div className="space-y-4 font-mono text-sm bg-black/40 rounded-lg p-4 border border-slate-800">
          <div className="flex justify-between items-center border-b border-slate-800 pb-3">
            <span className="text-slate-400 flex items-center gap-2">
              Daily Spend Ceiling Reached
              {isBudgetExceeded && <AlertTriangle className="w-4 h-4 text-rose-500 animate-pulse" />}
            </span>
            <span className={`font-bold ${isBudgetExceeded ? "text-rose-400" : "text-emerald-400"}`}>
              {isBudgetExceeded ? "TRUE - FACTORY HALTED" : "FALSE - CLEAR"}
            </span>
          </div>
          <div className="flex justify-between items-center border-b border-slate-800 pb-3">
            <span className="text-slate-400 flex items-center gap-2">
              <Wallet className="w-3.5 h-3.5" /> Ledger Balance
            </span>
            <span className="text-emerald-400 font-bold">
              ${(metrics.balance_cents / 100).toLocaleString()}
              <span className="text-slate-500 font-normal"> (+${(metrics.reinvest_pool_cents / 100).toLocaleString()} reinvest)</span>
            </span>
          </div>
          <div className="flex justify-between items-center">
            <span className="text-slate-400 flex items-center gap-2">
              <Clock className="w-3.5 h-3.5" /> Last Ledger Write
            </span>
            <span className="text-slate-200 font-bold" suppressHydrationWarning>
              {state?.last_ledger_write ? timeAgo(state.last_ledger_write) : "never"}
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}

function MetricCard({
  title,
  value,
  subtitle,
  icon: Icon,
  color = "slate",
}: {
  title: string;
  value: string;
  subtitle?: string;
  icon: LucideIcon;
  color?: "slate" | "emerald" | "rose";
}) {
  const colors: Record<string, string> = {
    slate: "text-slate-900 bg-slate-100 text-slate-600",
    emerald: "text-emerald-900 bg-emerald-100 text-emerald-600",
    rose: "text-rose-900 bg-rose-100 text-rose-600",
  };

  const [text, bg, iconColor] = colors[color].split(" ");

  return (
    <div className="bg-white p-6 rounded-xl border border-slate-200 shadow-sm">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-sm font-medium text-slate-500">{title}</h3>
        <div className={`p-2 rounded-lg ${bg}`}>
          <Icon className={`w-5 h-5 ${iconColor}`} />
        </div>
      </div>
      <div className={`text-3xl font-bold tracking-tight ${text}`} suppressHydrationWarning>{value}</div>
      {subtitle && <div className="text-sm text-slate-500 mt-2">{subtitle}</div>}
    </div>
  );
}
