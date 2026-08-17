import { useFactoryStream } from "@/hooks/use-factory-stream";
import { CheckCircle2, XCircle, AlertTriangle, MessageCircle } from "lucide-react";
import { motion } from "motion/react";

function timeAgo(iso: string): string {
  const min = Math.floor((Date.now() - new Date(iso).getTime()) / 60000);
  if (min < 1) return "just now";
  if (min < 60) return `${min}m ago`;
  const hrs = Math.floor(min / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

export function Interrupts() {
  const { interrupts } = useFactoryStream();

  return (
    <div className="space-y-6 max-w-4xl">
      <div>
        <h2 className="text-2xl font-bold tracking-tight text-slate-900">Escalations & Interrupts</h2>
        <p className="text-sm text-slate-500 mt-1">
          Tasks blocked at safety boundaries awaiting manual human resolution via Telegram.
        </p>
      </div>

      {interrupts.length === 0 ? (
        <div className="bg-emerald-50 border border-emerald-200 rounded-xl p-8 flex flex-col items-center justify-center text-center">
          <CheckCircle2 className="w-12 h-12 text-emerald-400 mb-3" />
          <h3 className="text-lg font-semibold text-emerald-800">Clear Pipeline</h3>
          <p className="text-sm text-emerald-600">No active escalations. The factory is fully autonomous.</p>
        </div>
      ) : (
        <div className="space-y-4">
          {interrupts.map((int) => (
            <motion.div
              key={int.id}
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              className="bg-white rounded-xl border border-amber-200 shadow-sm overflow-hidden"
            >
              <div className="bg-amber-50 border-amber-100 p-4 border-b flex items-center justify-between">
                <div className="flex items-center gap-3">
                  <AlertTriangle className="w-5 h-5 text-amber-500" />
                  <div>
                    <h3 className="font-bold text-amber-900">{int.reason.replace(/_/g, " ")}</h3>
                    <div className="text-xs font-mono mt-0.5 text-amber-700">
                      {int.subject}
                      {int.product_id && <> • Product: {int.product_id}</>}
                    </div>
                  </div>
                </div>
                <div className="text-[10px] text-slate-500 font-mono" suppressHydrationWarning>
                  {timeAgo(int.created_at)}
                </div>
              </div>

              <div className="p-6">
                {int.headline && <p className="text-sm font-semibold text-slate-800 mb-2">{int.headline}</p>}
                <p className="text-sm text-slate-700 mb-4 whitespace-pre-wrap">{int.detail}</p>

                {int.reply_hint && (
                  <div className="flex items-start gap-2 bg-slate-50 border border-slate-100 rounded-lg p-3 mb-6 text-xs text-slate-600">
                    <MessageCircle className="w-3.5 h-3.5 mt-0.5 shrink-0 text-slate-400" />
                    <div>
                      <span className="font-semibold text-slate-500 uppercase tracking-wide text-[10px] block mb-0.5">Reply hint</span>
                      {int.reply_hint}
                    </div>
                  </div>
                )}

                <div className="flex items-center gap-3">
                  <button
                    disabled
                    title="Read-only dashboard — resolve via Telegram"
                    className="px-4 py-2 bg-rose-100 text-rose-400 text-sm font-semibold rounded-lg flex items-center gap-2 cursor-not-allowed"
                  >
                    <CheckCircle2 className="w-4 h-4" />
                    Approve (Override)
                  </button>
                  <button
                    disabled
                    title="Read-only dashboard — resolve via Telegram"
                    className="px-4 py-2 bg-white border border-slate-200 text-slate-400 text-sm font-semibold rounded-lg flex items-center gap-2 cursor-not-allowed"
                  >
                    <XCircle className="w-4 h-4" />
                    Reject & Requeue
                  </button>
                  <span className="text-xs text-slate-400">Respond in Telegram to resolve — this view is read-only.</span>
                </div>
              </div>
            </motion.div>
          ))}
        </div>
      )}
    </div>
  );
}
