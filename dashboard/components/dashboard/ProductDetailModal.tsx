"use client";

import { useProductDetail } from "@/hooks/use-product-detail";
import {
  ExternalLink,
  FileText,
  Loader2,
  Palette,
  Sparkles,
  X,
} from "lucide-react";
import { AnimatePresence, motion } from "motion/react";

const DIMENSIONS: { key: "pain_severity" | "willingness_to_pay" | "build_feasibility" | "adoption_ease" | "defensibility"; label: string }[] = [
  { key: "pain_severity", label: "Pain severity" },
  { key: "willingness_to_pay", label: "Willingness to pay" },
  { key: "build_feasibility", label: "Build feasibility" },
  { key: "adoption_ease", label: "Adoption ease" },
  { key: "defensibility", label: "Defensibility" },
];

export function ProductDetailModal({
  productId,
  onClose,
}: {
  productId: string;
  onClose: () => void;
}) {
  const { detail, loading, notFound, error, generating, generateDesignBrief } =
    useProductDetail(productId);

  return (
    <AnimatePresence>
      <motion.div
        key="backdrop"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        onClick={onClose}
        className="fixed inset-0 bg-slate-900/40 backdrop-blur-sm z-40 flex items-center justify-center p-4"
      >
        <motion.div
          layoutId={`card-${productId}`}
          onClick={(event) => event.stopPropagation()}
          className="bg-white rounded-2xl border border-slate-200 shadow-xl w-full max-w-2xl max-h-[85vh] overflow-y-auto"
        >
          {loading && !detail && (
            <div className="p-8 text-sm text-slate-500">Loading product...</div>
          )}

          {notFound && (
            <div className="p-8">
              <div className="flex justify-end">
                <CloseButton onClose={onClose} />
              </div>
              <p className="text-sm text-slate-500">No product with that id.</p>
            </div>
          )}

          {error && !detail && (
            <div className="p-8">
              <div className="flex justify-end">
                <CloseButton onClose={onClose} />
              </div>
              <p className="text-sm text-rose-600">{error}</p>
            </div>
          )}

          {detail && (
            <div className="p-6 space-y-6">
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0">
                  <div className="text-xs font-mono text-slate-400 mb-1">{detail.id}</div>
                  <h2 className="text-xl font-bold text-slate-900 leading-tight">{detail.name}</h2>
                  <span className="inline-block mt-2 text-xs font-semibold px-2 py-0.5 rounded-full bg-slate-100 text-slate-600 uppercase tracking-wide">
                    {detail.status}
                  </span>
                </div>
                <CloseButton onClose={onClose} />
              </div>

              <section>
                <SectionHeader icon={<FileText className="w-4 h-4 text-indigo-600" />} title="Business plan" />
                <div className="bg-slate-50 border border-slate-200 rounded-xl p-4 space-y-3">
                  <Field label="Niche" value={detail.business_plan.target_niche} />
                  <Field label="Problem" value={detail.business_plan.problem_statement} />
                  <Field label="Monetization" value={detail.business_plan.monetization_hypothesis} />
                  <div className="flex flex-wrap gap-4 pt-1">
                    <MiniStat label="Price" value={`$${detail.business_plan.self_serve_price_usd}/mo`} />
                    <MiniStat label="Score" value={detail.business_plan.score.toFixed(2)} />
                    <MiniStat label="Evidence" value={`${detail.business_plan.evidence_count} signal(s)`} />
                  </div>
                  <div className="grid grid-cols-2 gap-x-6 gap-y-2 pt-2">
                    {DIMENSIONS.filter((d) => detail.business_plan[d.key] !== null).map((d) => (
                      <DimensionBar
                        key={d.key}
                        label={d.label}
                        value={detail.business_plan[d.key] as number}
                      />
                    ))}
                  </div>
                  <a
                    href={detail.business_plan.source_url}
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex items-center gap-1 text-xs text-indigo-600 hover:text-indigo-800 pt-1"
                  >
                    Source signal <ExternalLink className="w-3 h-3" />
                  </a>
                </div>
              </section>

              <section>
                <SectionHeader icon={<Palette className="w-4 h-4 text-rose-600" />} title="Visual design guidelines" />
                {detail.design_brief ? (
                  <div className="bg-slate-50 border border-slate-200 rounded-xl p-4 space-y-3">
                    <div className="flex gap-2">
                      {detail.design_brief.palette.map((color) => (
                        <div key={color} className="flex flex-col items-center gap-1">
                          <div
                            className="w-8 h-8 rounded-lg border border-slate-200 shadow-sm"
                            style={{ backgroundColor: color }}
                          />
                          <span className="text-[10px] font-mono text-slate-500">{color}</span>
                        </div>
                      ))}
                    </div>
                    <Field label="Typography" value={detail.design_brief.typography} />
                    <Field label="Tone" value={detail.design_brief.tone} />
                  </div>
                ) : (
                  <div className="bg-slate-50 border border-dashed border-slate-300 rounded-xl p-4 flex items-center justify-between gap-3">
                    <p className="text-xs text-slate-500">
                      {detail.deploy_url
                        ? "Not generated yet."
                        : "This product isn't live yet -- nothing to describe."}
                    </p>
                    {detail.deploy_url && (
                      <button
                        onClick={generateDesignBrief}
                        disabled={generating}
                        className="shrink-0 flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold text-indigo-700 bg-indigo-50 hover:bg-indigo-100 rounded-lg transition-colors disabled:opacity-50"
                      >
                        {generating ? (
                          <Loader2 className="w-3.5 h-3.5 animate-spin" />
                        ) : (
                          <Sparkles className="w-3.5 h-3.5" />
                        )}
                        {generating ? "Generating..." : "Generate design brief"}
                      </button>
                    )}
                  </div>
                )}
              </section>

              <section>
                <SectionHeader icon={<ExternalLink className="w-4 h-4 text-emerald-600" />} title="Live version" />
                {detail.deploy_url ? (
                  <a
                    href={detail.deploy_url}
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex items-center gap-2 px-4 py-2 bg-emerald-600 hover:bg-emerald-700 text-white text-sm font-semibold rounded-lg transition-colors"
                  >
                    Open live site <ExternalLink className="w-4 h-4" />
                  </a>
                ) : (
                  <p className="text-xs text-slate-500">Not live yet.</p>
                )}
              </section>
            </div>
          )}
        </motion.div>
      </motion.div>
    </AnimatePresence>
  );
}

function CloseButton({ onClose }: { onClose: () => void }) {
  return (
    <button onClick={onClose} className="shrink-0 text-slate-400 hover:text-slate-600">
      <X className="w-5 h-5" />
    </button>
  );
}

function SectionHeader({ icon, title }: { icon: React.ReactNode; title: string }) {
  return (
    <h3 className="font-bold text-slate-800 flex items-center gap-2 mb-2 text-sm">
      {icon}
      {title}
    </h3>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-[10px] font-semibold uppercase tracking-wide text-slate-400">{label}</div>
      <p className="text-sm text-slate-700 mt-0.5">{value}</p>
    </div>
  );
}

function MiniStat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-[10px] font-semibold uppercase tracking-wide text-slate-400">{label}</div>
      <div className="text-sm font-bold text-slate-800">{value}</div>
    </div>
  );
}

function DimensionBar({ label, value }: { label: string; value: number }) {
  return (
    <div>
      <div className="flex justify-between text-[10px] text-slate-500 mb-0.5">
        <span>{label}</span>
        <span className="font-mono">{value.toFixed(2)}</span>
      </div>
      <div className="h-1.5 bg-slate-200 rounded-full overflow-hidden">
        <div
          className="h-full bg-indigo-500 rounded-full"
          style={{ width: `${Math.round(value * 100)}%` }}
        />
      </div>
    </div>
  );
}
