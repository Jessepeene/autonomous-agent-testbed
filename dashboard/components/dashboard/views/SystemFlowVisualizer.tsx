import { motion, AnimatePresence } from "motion/react";
import { useFactoryStream, type Product } from "@/hooks/use-factory-stream";
import { Code2, Server, BrainCircuit, Play, DollarSign, FlaskConical, Activity, Mail, Target, Database } from "lucide-react";
import type { LucideIcon } from "lucide-react";

export function SystemFlowVisualizer() {
  const { metrics, marketing, products, activity, trials, tasks } = useFactoryStream();

  if (!metrics || !marketing) {
    return <div className="w-full h-[600px] bg-slate-950 rounded-xl animate-pulse" />;
  }

  // Real ProductStatus values -- Discovery/Compliance aren't Product-stage
  // concepts (they happen at the Idea stage, before a Product row exists),
  // so those two nodes are repurposed below rather than fed fake buckets.
  const pValidating = products.filter((p) => p.status === "validating");
  const pPlanned = products.filter((p) => p.status === "planned");
  const pBuild = products.filter(
    (p) => p.status === "building" || p.status === "reviewing" || p.status === "patching",
  );
  const pLive = products.filter((p) => p.status === "live");

  // Real Idea-stage signal for the "Market Intel" node -- ideas haven't
  // become products yet, so there's no product bucket for this.
  const ideaEntries = Object.entries(metrics.ideas_by_status).filter(([, count]) => count > 0);
  const ideaTotal = ideaEntries.reduce((sum, [, count]) => sum + count, 0);

  const pendingTasks = metrics.tasks_by_status.pending ?? 0;

  const displayActivity = [...activity].slice(0, 4).reverse();

  return (
    <div className="relative w-full md:aspect-[4/3] md:max-h-[750px] bg-[#030712] rounded-xl border border-slate-900 p-3 md:p-8 overflow-y-auto overflow-x-hidden md:overflow-hidden font-mono text-slate-300 shadow-[0_0_40px_rgba(59,130,246,0.05)]">
       {/* Background connections SVG (Desktop only) */}
       <div className="hidden lg:block absolute inset-0">
         <FlowBackground />
       </div>

       {/* The Factory Control Loop Grid */}
       <div className="grid grid-cols-2 lg:grid-cols-3 lg:grid-rows-3 gap-3 md:gap-8 w-full md:h-full relative z-10">

          {/* Center Core: Supervisor Orchestrator (Top on Mobile) */}
          <div className="col-span-2 lg:col-span-1 order-first lg:order-none lg:col-start-2 lg:row-start-2 flex flex-col bg-slate-900/80 backdrop-blur-md border border-cyan-500/50 shadow-[0_0_30px_rgba(6,182,212,0.15)] rounded-xl p-3 h-[220px] lg:h-full relative group">
            <div className="absolute inset-0 bg-cyan-500/5 pointer-events-none group-hover:bg-cyan-500/10 transition-colors rounded-xl" />
            <div className="flex items-center justify-between mb-2 text-xs font-bold text-cyan-400 tracking-widest relative z-10">
               <span className="flex items-center gap-2"><Database className="w-4 h-4" /> SUPERVISOR DB</span>
               <span className="text-[10px]">Q:{pendingTasks}</span>
            </div>

            {/* Live Terminal -- synthesized from real DB rows (tasks, interrupts), not a log tail */}
            <div className="flex-1 bg-black/80 rounded border border-slate-800 p-2 overflow-hidden flex flex-col justify-end text-[9px] sm:text-[10px] text-slate-400 relative z-10">
              <div className="absolute inset-0 bg-gradient-to-t from-black via-transparent to-transparent z-10 pointer-events-none" />
              <div className="flex flex-col gap-1 z-0">
                {displayActivity.map((item) => (
                  <motion.div key={item.id} initial={{ opacity: 0, x: -5 }} animate={{ opacity: 1, x: 0 }} className="whitespace-nowrap truncate">
                    <span className="text-cyan-500 mr-1.5">{'>'}</span>{item.message}
                  </motion.div>
                ))}
              </div>
            </div>

            {/* Running Tasks ticker */}
            <div className="mt-2 text-[9px] flex gap-1 overflow-hidden relative z-10">
              {tasks.filter((t) => t.status === "running").map((t) => (
                <span key={t.id} className="bg-cyan-950 text-cyan-400 px-1 rounded truncate border border-cyan-900">{t.id}</span>
              ))}
            </div>
          </div>

          {/* Row 1 */}
          <div className="lg:col-start-1 lg:row-start-1">
            <NodeCard title="Market Intel" icon={Target} color="purple">
              <div className="flex flex-col h-full justify-center items-center text-center gap-1">
                <span className="text-[10px] text-slate-500 uppercase">Ideas in Pipeline</span>
                <span className="text-purple-400 font-bold text-lg sm:text-xl">{ideaTotal}</span>
              </div>
              <div className="mt-auto pt-2 text-[9px] text-purple-400/80 flex flex-wrap gap-x-2 gap-y-0.5">
                {ideaEntries.map(([status, count]) => (
                  <span key={status} className="capitalize">{status}:{count}</span>
                ))}
              </div>
            </NodeCard>
          </div>

          <div className="lg:col-start-2 lg:row-start-1">
            <NodeCard title="Planned" icon={Play} color="indigo">
              <ProductContainer products={pPlanned} color="indigo" />
              <div className="mt-auto pt-2 text-[10px] text-indigo-400 font-bold flex justify-between">
                <span>Queued to build</span>
                <span>{pPlanned.length}</span>
              </div>
            </NodeCard>
          </div>

          <div className="lg:col-start-3 lg:row-start-1">
            <NodeCard title="Validating" icon={FlaskConical} color="blue">
              <ProductContainer products={pValidating} color="blue" />
              <div className="mt-auto pt-2 text-[10px] text-blue-400 font-bold flex justify-between">
                <span>Running LPs</span>
                <span>{pValidating.length}</span>
              </div>
            </NodeCard>
          </div>

          {/* Row 2 */}
          <div className="lg:col-start-1 lg:row-start-2">
            <NodeCard title="Learning Loop" icon={BrainCircuit} color="pink">
              <div className="flex flex-col gap-2 h-full justify-center">
                <div className="text-[10px] text-slate-500 uppercase">Active Trials</div>
                <div className="flex gap-1 flex-wrap">
                  {trials.map((t) => (
                    <motion.div key={t.id} animate={{ opacity: [0.5, 1, 0.5] }} transition={{ repeat: Infinity, duration: 2 }} className="w-2 h-2 rounded-full bg-pink-500" />
                  ))}
                </div>
              </div>
            </NodeCard>
          </div>

          <div className="lg:col-start-3 lg:row-start-2">
            <NodeCard title="Factory VM" icon={Code2} color="sky">
              <ProductContainer products={pBuild} color="sky" />
              <div className="mt-auto pt-2 text-[10px] text-sky-400 font-bold flex justify-between">
                <span>In Development</span>
                {pBuild.length > 0 && <Activity className="w-3 h-3 animate-pulse" />}
              </div>
            </NodeCard>
          </div>

          {/* Row 3 */}
          <div className="lg:col-start-1 lg:row-start-3">
            <NodeCard title="Revenue Ledger" icon={DollarSign} color="amber">
              <div className="flex flex-col h-full justify-center items-center text-center">
                <span className="text-[10px] text-slate-500 uppercase mb-1">Live MRR</span>
                <motion.div
                  key={metrics.mrr_cents}
                  animate={{ scale: [1.2, 1], color: ["#fbbf24", "#fbbf24"] }}
                  className="text-amber-400 font-bold text-lg sm:text-xl"
                  suppressHydrationWarning
                >
                  ${(metrics.mrr_cents / 100).toLocaleString()}
                </motion.div>
              </div>
            </NodeCard>
          </div>

          <div className="lg:col-start-2 lg:row-start-3">
            <NodeCard title="Marketing" icon={Mail} color="emerald">
              <div className="flex flex-col h-full justify-center items-center text-center">
                <span className="text-[10px] text-slate-500 uppercase mb-1">Outreach Fleet</span>
                <motion.div
                  key={marketing.outreach_sent}
                  animate={{ scale: [1.2, 1], color: ["#34d399", "#34d399"] }}
                  className="text-emerald-400 font-bold text-lg sm:text-xl"
                  suppressHydrationWarning
                >
                  {marketing.outreach_sent.toLocaleString()}
                </motion.div>
              </div>
            </NodeCard>
          </div>

          <div className="lg:col-start-3 lg:row-start-3">
            <NodeCard title="Live Hosting" icon={Server} color="emerald">
              <ProductContainer products={pLive} color="emerald" />
              <div className="mt-auto pt-2 text-[10px] text-emerald-400 font-bold flex justify-between items-center">
                <span>Traffic</span>
                <motion.div animate={{ opacity: [0.2, 1, 0.2] }} transition={{ repeat: Infinity, duration: 1.5 }} className="flex gap-0.5">
                  {[1,2,3].map(i => <span key={i} className="w-1 h-3 bg-emerald-500 rounded-sm" />)}
                </motion.div>
              </div>
            </NodeCard>
          </div>
       </div>
    </div>
  );
}

// -------------------------------------------------------------
// Sub-components
// -------------------------------------------------------------

function NodeCard({
  title,
  icon: Icon,
  color,
  children,
}: {
  title: string;
  icon: LucideIcon;
  color: string;
  children: React.ReactNode;
}) {
  const colorMap: Record<string, string> = {
    purple: "border-purple-900/50 text-purple-400 shadow-[0_0_15px_rgba(168,85,247,0.05)]",
    indigo: "border-indigo-900/50 text-indigo-400 shadow-[0_0_15px_rgba(99,102,241,0.05)]",
    blue: "border-blue-900/50 text-blue-400 shadow-[0_0_15px_rgba(59,130,246,0.05)]",
    sky: "border-sky-900/50 text-sky-400 shadow-[0_0_15px_rgba(14,165,233,0.05)]",
    emerald: "border-emerald-900/50 text-emerald-400 shadow-[0_0_15px_rgba(16,185,129,0.05)]",
    amber: "border-amber-900/50 text-amber-400 shadow-[0_0_15px_rgba(245,158,11,0.05)]",
    pink: "border-pink-900/50 text-pink-400 shadow-[0_0_15px_rgba(236,72,153,0.05)]",
  };

  return (
    <div className={`flex flex-col bg-black/60 backdrop-blur-md border rounded-xl p-3 min-h-[120px] lg:min-h-0 h-full relative group ${colorMap[color]}`}>
      <div className="flex items-center gap-2 mb-2 text-xs font-bold uppercase tracking-widest relative z-10">
         <Icon className="w-4 h-4" />
         <span className="truncate">{title}</span>
      </div>
      <div className="flex-1 flex flex-col relative z-10">
        {children}
      </div>
    </div>
  );
}

function ProductContainer({ products, color }: { products: Product[]; color: string }) {
  const bgMap: Record<string, string> = {
    purple: "bg-purple-500/10 text-purple-300 border-purple-500/20",
    indigo: "bg-indigo-500/10 text-indigo-300 border-indigo-500/20",
    blue: "bg-blue-500/10 text-blue-300 border-blue-500/20",
    sky: "bg-sky-500/10 text-sky-300 border-sky-500/20",
    emerald: "bg-emerald-500/10 text-emerald-300 border-emerald-500/20",
  };

  return (
    <div className="w-full h-full flex flex-wrap gap-1 content-start">
      <AnimatePresence>
        {products.map((p) => (
          <motion.div
            layoutId={`vis-${p.id}`}
            key={p.id}
            className={`text-[9px] px-1.5 py-0.5 rounded border font-mono whitespace-nowrap shadow-sm relative z-50 ${bgMap[color]}`}
          >
            {p.name.substring(0, 10)}
          </motion.div>
        ))}
      </AnimatePresence>
    </div>
  );
}

// -------------------------------------------------------------
// SVG Background (The animated grid lines connecting the nodes)
// -------------------------------------------------------------

function FlowBackground() {
  return (
    <svg className="absolute inset-0 w-full h-full pointer-events-none" preserveAspectRatio="none">
      <defs>
        <linearGradient id="cyan-glow" x1="0%" y1="0%" x2="100%" y2="100%">
          <stop offset="0%" stopColor="#06b6d4" stopOpacity="0.1" />
          <stop offset="50%" stopColor="#06b6d4" stopOpacity="0.4" />
          <stop offset="100%" stopColor="#06b6d4" stopOpacity="0.1" />
        </linearGradient>
      </defs>

      {/* Main Ring Path connecting the outer 8 nodes */}
      <rect
        x="16.66%" y="16.66%"
        width="66.66%" height="66.66%"
        fill="none"
        stroke="#0f172a"
        strokeWidth="2"
        rx="20"
      />

      {/* Animated Pulses on the Ring */}
      <motion.rect
        x="16.66%" y="16.66%"
        width="66.66%" height="66.66%"
        fill="none"
        stroke="url(#cyan-glow)"
        strokeWidth="2"
        rx="20"
        strokeDasharray="100 200"
        animate={{ strokeDashoffset: [300, 0] }}
        transition={{ repeat: Infinity, duration: 8, ease: "linear" }}
      />
      <motion.rect
        x="16.66%" y="16.66%"
        width="66.66%" height="66.66%"
        fill="none"
        stroke="#38bdf8"
        strokeWidth="2"
        rx="20"
        strokeDasharray="20 400"
        animate={{ strokeDashoffset: [0, -420] }}
        transition={{ repeat: Infinity, duration: 5, ease: "linear" }}
      />

      {/* Spokes connecting to the Center (Supervisor) */}
      {[
        { x1: "16.66%", y1: "16.66%" }, // Top Left
        { x1: "50%", y1: "16.66%" },    // Top Mid
        { x1: "83.33%", y1: "16.66%" }, // Top Right
        { x1: "83.33%", y1: "50%" },    // Mid Right
        { x1: "83.33%", y1: "83.33%" }, // Bottom Right
        { x1: "50%", y1: "83.33%" },    // Bottom Mid
        { x1: "16.66%", y1: "83.33%" }, // Bottom Left
        { x1: "16.66%", y1: "50%" },    // Mid Left
      ].map((pos, i) => (
        <g key={i}>
          <line x1={pos.x1} y1={pos.y1} x2="50%" y2="50%" stroke="#0f172a" strokeWidth="2"/>
          <motion.line
            x1={pos.x1} y1={pos.y1} x2="50%" y2="50%"
            stroke="#0ea5e9" strokeWidth="1" strokeOpacity="0.4"
            strokeDasharray="5 15"
            animate={{ strokeDashoffset: [20, 0] }}
            transition={{ repeat: Infinity, duration: 1, ease: "linear" }}
          />
        </g>
      ))}
    </svg>
  );
}
