"use client";

import { useState } from "react";
import { useFactoryStream } from "@/hooks/use-factory-stream";
import type { Interrupt } from "@/hooks/use-factory-stream";
import { CheckCircle2, XCircle, AlertTriangle, MessageCircle, Send } from "lucide-react";
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
  const { interrupts, refresh } = useFactoryStream();

  return (
    <div className="space-y-6 max-w-4xl">
      <div>
        <h2 className="text-2xl font-bold tracking-tight text-slate-900">Escalations & Interrupts</h2>
        <p className="text-sm text-slate-500 mt-1">
          Tasks blocked at safety boundaries awaiting a reply — same resolution path as Telegram.
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
            <InterruptCard key={int.id} interrupt={int} onResolved={refresh} />
          ))}
        </div>
      )}
    </div>
  );
}

function InterruptCard({ interrupt, onResolved }: { interrupt: Interrupt; onResolved: () => void }) {
  const [reply, setReply] = useState("");
  const [submitting, setSubmitting] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function send(response: string) {
    if (!response.trim() || submitting) return;
    setSubmitting(response);
    setError(null);
    try {
      const res = await fetch(`/api/interrupts/${encodeURIComponent(interrupt.id)}/resolve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ response: response.trim() }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`);
      }
      setReply("");
      onResolved();
    } catch (err) {
      setError(err instanceof Error ? err.message : "failed to send reply");
    } finally {
      setSubmitting(null);
    }
  }

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      className="bg-white rounded-xl border border-amber-200 shadow-sm overflow-hidden"
    >
      <div className="bg-amber-50 border-amber-100 p-4 border-b flex items-center justify-between">
        <div className="flex items-center gap-3">
          <AlertTriangle className="w-5 h-5 text-amber-500" />
          <div>
            <h3 className="font-bold text-amber-900">{interrupt.reason.replace(/_/g, " ")}</h3>
            <div className="text-xs font-mono mt-0.5 text-amber-700">
              {interrupt.subject}
              {interrupt.product_id && <> • Product: {interrupt.product_id}</>}
            </div>
          </div>
        </div>
        <div className="text-[10px] text-slate-500 font-mono" suppressHydrationWarning>
          {timeAgo(interrupt.created_at)}
        </div>
      </div>

      <div className="p-6">
        {interrupt.headline && <p className="text-sm font-semibold text-slate-800 mb-2">{interrupt.headline}</p>}
        <p className="text-sm text-slate-700 mb-4 whitespace-pre-wrap">{interrupt.detail}</p>

        {interrupt.reply_hint && (
          <div className="flex items-start gap-2 bg-slate-50 border border-slate-100 rounded-lg p-3 mb-4 text-xs text-slate-600">
            <MessageCircle className="w-3.5 h-3.5 mt-0.5 shrink-0 text-slate-400" />
            <div>
              <span className="font-semibold text-slate-500 uppercase tracking-wide text-[10px] block mb-0.5">Reply hint</span>
              {interrupt.reply_hint}
            </div>
          </div>
        )}

        <div className="flex items-center gap-3 mb-3">
          <button
            onClick={() => send("approve")}
            disabled={submitting !== null}
            className="px-3 py-1.5 bg-emerald-50 hover:bg-emerald-100 text-emerald-700 text-xs font-semibold rounded-lg flex items-center gap-1.5 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            <CheckCircle2 className="w-3.5 h-3.5" />
            {submitting === "approve" ? "Sending..." : "Approve"}
          </button>
          <button
            onClick={() => send("reject")}
            disabled={submitting !== null}
            className="px-3 py-1.5 bg-rose-50 hover:bg-rose-100 text-rose-700 text-xs font-semibold rounded-lg flex items-center gap-1.5 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            <XCircle className="w-3.5 h-3.5" />
            {submitting === "reject" ? "Sending..." : "Reject"}
          </button>
        </div>

        <form
          onSubmit={(event) => {
            event.preventDefault();
            send(reply);
          }}
          className="flex items-start gap-2"
        >
          <textarea
            value={reply}
            onChange={(event) => setReply(event.target.value)}
            placeholder="Type a reply, following the hint above..."
            rows={2}
            disabled={submitting !== null}
            className="flex-1 text-sm rounded-lg border border-slate-200 px-3 py-2 outline-none focus:border-indigo-400 disabled:opacity-50 resize-none"
          />
          <button
            type="submit"
            disabled={submitting !== null || !reply.trim()}
            className="px-4 py-2 bg-indigo-600 hover:bg-indigo-700 text-white text-sm font-semibold rounded-lg flex items-center gap-2 disabled:opacity-50 disabled:cursor-not-allowed transition-colors shrink-0"
          >
            <Send className="w-4 h-4" />
            {submitting !== null && submitting !== "approve" && submitting !== "reject" ? "Sending..." : "Send"}
          </button>
        </form>
        {error && <p className="text-xs text-rose-600 mt-2">{error}</p>}
      </div>
    </motion.div>
  );
}
