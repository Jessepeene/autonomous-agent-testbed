import { useFactoryStream } from "@/hooks/use-factory-stream";
import { Mail, Users, AlertTriangle, Activity, Snowflake, ShieldOff } from "lucide-react";
import { motion } from "motion/react";
import type { LucideIcon } from "lucide-react";

export function GrowthOutreach() {
  const { marketing } = useFactoryStream();

  if (!marketing) {
    return <div className="text-sm text-slate-500">Loading growth & outreach...</div>;
  }

  const sent30d = marketing.deliverability_sent_30d;
  const complaintRate = sent30d > 0 ? marketing.deliverability_complained_30d / sent30d : 0;
  const bounceRate = sent30d > 0 ? marketing.deliverability_bounced_30d / sent30d : 0;
  const complaintPct = Math.min(100, (complaintRate / marketing.spam_complaint_rate_threshold) * 100);
  const bouncePct = Math.min(100, (bounceRate / marketing.bounce_rate_threshold) * 100);

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-bold tracking-tight text-slate-900">Growth & Outreach</h2>
        <p className="text-sm text-slate-500 mt-1">Marketing execution and deliverability circuit breaker</p>
      </div>

      {marketing.freeze_active && (
        <div className="flex items-center gap-3 p-4 bg-rose-50 border border-rose-300 rounded-xl text-rose-800">
          <Snowflake className="w-5 h-5 shrink-0" />
          <div>
            <div className="font-bold text-sm">Outbound engines frozen</div>
            <div className="text-xs">{marketing.freeze_reason ?? "No reason recorded."}</div>
          </div>
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
        <MetricCard title="Outreach Sent" value={marketing.outreach_sent.toLocaleString()} icon={Mail} color="blue" />
        <MetricCard title="Leads Total" value={marketing.leads_total.toLocaleString()} icon={Users} color="indigo" />
        <MetricCard title="Suppressed (no-call list)" value={marketing.outreach_suppressed.toLocaleString()} icon={ShieldOff} color="indigo" />
      </div>

      <div className="bg-white rounded-xl p-6 border border-slate-200 shadow-sm">
        <div className="flex items-center justify-between mb-4">
          <h3 className="font-bold text-slate-800 flex items-center gap-2">
            <Activity className="w-5 h-5 text-indigo-600" />
            Deliverability Circuit Breaker (trailing 30d, {sent30d.toLocaleString()} sent)
          </h3>
        </div>

        <div className="space-y-6">
          <div>
            <div className="flex justify-between text-sm mb-2">
              <span className="text-slate-600 font-medium">Complaint Rate</span>
              <span className="text-slate-900 font-bold">
                {(complaintRate * 100).toFixed(3)}%
                <span className="text-slate-400 font-normal"> / {(marketing.spam_complaint_rate_threshold * 100).toFixed(3)}% threshold</span>
              </span>
            </div>
            <div className="w-full h-3 bg-slate-100 rounded-full overflow-hidden">
              <motion.div
                className={`h-full rounded-full ${complaintPct >= 100 ? "bg-rose-500" : complaintPct > 70 ? "bg-amber-500" : "bg-emerald-500"}`}
                initial={{ width: 0 }}
                animate={{ width: `${complaintPct}%` }}
              />
            </div>
          </div>

          <div>
            <div className="flex justify-between text-sm mb-2">
              <span className="text-slate-600 font-medium">Bounce Rate</span>
              <span className="text-slate-900 font-bold">
                {(bounceRate * 100).toFixed(2)}%
                <span className="text-slate-400 font-normal"> / {(marketing.bounce_rate_threshold * 100).toFixed(2)}% threshold</span>
              </span>
            </div>
            <div className="w-full h-3 bg-slate-100 rounded-full overflow-hidden">
              <motion.div
                className={`h-full rounded-full ${bouncePct >= 100 ? "bg-rose-500" : bouncePct > 70 ? "bg-amber-500" : "bg-emerald-500"}`}
                initial={{ width: 0 }}
                animate={{ width: `${bouncePct}%` }}
              />
            </div>
          </div>

          <div className="flex items-center justify-between p-4 bg-slate-50 rounded-lg border border-slate-100">
            <div className="flex items-center gap-3">
              <AlertTriangle className={`w-5 h-5 ${marketing.deliverability_complained_30d > 0 ? "text-amber-500" : "text-slate-300"}`} />
              <div>
                <div className="font-medium text-slate-800">Complaints (30d)</div>
                <div className="text-xs text-slate-500">{marketing.deliverability_bounced_30d.toLocaleString()} bounced over the same window</div>
              </div>
            </div>
            <div className="text-2xl font-bold text-slate-900">{marketing.deliverability_complained_30d}</div>
          </div>
        </div>
      </div>
    </div>
  );
}

function MetricCard({
  title,
  value,
  icon: Icon,
  color = "blue",
}: {
  title: string;
  value: string;
  icon: LucideIcon;
  color?: "blue" | "indigo";
}) {
  const colors: Record<string, string> = {
    blue: "text-blue-900 bg-blue-100 text-blue-600",
    indigo: "text-indigo-900 bg-indigo-100 text-indigo-600",
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
    </div>
  );
}
