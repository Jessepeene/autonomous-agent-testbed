import { useFactoryStream } from "@/hooks/use-factory-stream";
import type { ProductStage } from "@/hooks/use-factory-stream";
import { Clock } from "lucide-react";
import { motion } from "motion/react";

// Real ProductStatus values (state/schemas.py) -- Discovery/Compliance from
// the original mock aren't Product-stage concepts, they happen at the Idea
// stage before a Product row exists at all.
const STAGES: { id: ProductStage; name: string }[] = [
  { id: "validating", name: "Validating" },
  { id: "planned", name: "Planned" },
  { id: "building", name: "Building" },
  { id: "reviewing", name: "Reviewing" },
  { id: "patching", name: "Patching" },
  { id: "live", name: "Live" },
  { id: "retired", name: "Retired" },
];

export function PortfolioPipeline() {
  const { products } = useFactoryStream();

  const timeAgo = (date: Date) => {
    const min = Math.floor((new Date().getTime() - date.getTime()) / 60000);
    if (min < 60) return `${min}m ago`;
    const hrs = Math.floor(min / 60);
    if (hrs < 24) return `${hrs}h ago`;
    return `${Math.floor(hrs / 24)}d ago`;
  };

  return (
    <div className="h-full flex flex-col">
      <div className="mb-6">
        <h2 className="text-2xl font-bold tracking-tight text-slate-900">Product Pipeline</h2>
        <p className="text-sm text-slate-500 mt-1">Lifecycle state machine and product-level detail</p>
      </div>

      <div className="flex-1 flex gap-4 overflow-x-auto pb-4">
        {STAGES.map(stage => {
          const stageProducts = products.filter(p => p.status === stage.id);

          return (
            <div key={stage.id} className="flex-none w-80 bg-slate-50 rounded-xl border border-slate-200 flex flex-col h-full max-h-[800px]">
              <div className="p-3 border-b border-slate-200 bg-slate-100/50 flex justify-between items-center rounded-t-xl">
                <h3 className="font-semibold text-slate-800 text-sm flex items-center gap-2">
                  <div className="w-2 h-2 rounded-full bg-slate-400" />
                  {stage.name}
                </h3>
                <span className="bg-white text-slate-600 text-xs font-bold px-2 py-0.5 rounded-full border border-slate-200">
                  {stageProducts.length}
                </span>
              </div>

              <div className="p-3 flex-1 overflow-y-auto space-y-3">
                {stageProducts.map((product) => (
                  <motion.div
                    layoutId={`card-${product.id}`}
                    key={product.id}
                    className="bg-white rounded-lg border border-slate-200 shadow-sm p-3 relative overflow-hidden group"
                  >
                    <div className="flex justify-between items-start mb-2">
                      <div>
                        <div className="text-xs font-mono text-slate-400 mb-0.5">{product.id}</div>
                        <h4 className="font-bold text-slate-800 text-sm leading-tight">{product.name}</h4>
                      </div>
                    </div>

                    <div className="text-xs text-slate-500 mb-3 flex items-center gap-1.5 font-medium">
                      <span className="bg-slate-100 px-1.5 py-0.5 rounded">{product.niche}</span>
                      {product.heal_attempts > 0 && (
                        <span className="text-amber-600 bg-amber-50 px-1.5 py-0.5 rounded" title="Self-heal attempts">
                          {product.heal_attempts} heal{product.heal_attempts === 1 ? "" : "s"}
                        </span>
                      )}
                    </div>

                    <div className="flex items-center justify-between border-t border-slate-100 pt-3">
                      <div className="flex items-center gap-1.5 text-xs text-slate-500">
                        <Clock className="w-3 h-3" />
                        <span suppressHydrationWarning>{timeAgo(new Date(product.updated_at))} updated</span>
                      </div>

                      <div className="flex items-center gap-3">
                        <div className="flex flex-col text-right">
                          <span className="text-[9px] text-slate-400">SCORE</span>
                          <span className="text-xs font-bold text-indigo-600">{product.idea_score}</span>
                        </div>
                        {product.mrr_cents > 0 && (
                          <div className="flex flex-col text-right">
                            <span className="text-[9px] text-slate-400">MRR</span>
                            <span className="text-xs font-bold text-emerald-600">${(product.mrr_cents / 100).toLocaleString()}</span>
                          </div>
                        )}
                      </div>
                    </div>
                  </motion.div>
                ))}

                {stageProducts.length === 0 && (
                  <div className="text-center py-6 border-2 border-dashed border-slate-200 rounded-lg text-slate-400 text-sm">
                    Queue Empty
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
