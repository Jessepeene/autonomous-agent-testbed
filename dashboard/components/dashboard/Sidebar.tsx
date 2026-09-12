"use client";

import {
  Activity,
  ListTodo,
  AlertTriangle,
  TerminalSquare,
  Network,
  Banknote,
  Megaphone,
  KeyRound,
  LogOut,
} from "lucide-react";
import { useRouter } from "next/navigation";
import { useFactoryStream } from "@/hooks/use-factory-stream";
import { useIntegrations } from "@/hooks/use-integrations";

interface SidebarProps {
  activeTab: string;
  setActiveTab: (tab: string) => void;
}

export function Sidebar({ activeTab, setActiveTab }: SidebarProps) {
  const router = useRouter();
  const { health, metrics } = useFactoryStream();
  const { alerts } = useIntegrations();
  const openInterrupts = metrics?.open_interrupts ?? 0;

  const tabs = [
    { id: "health", label: "System Health", icon: Activity },
    { id: "orchestration", label: "Task Orchestration", icon: Network },
    { id: "pipeline", label: "Product Pipeline", icon: ListTodo },
    { id: "finance", label: "Finance & Spend", icon: Banknote },
    { id: "growth", label: "Growth & Outreach", icon: Megaphone },
    { id: "learning", label: "Learning Trials", icon: TerminalSquare },
    {
      id: "interrupts",
      label: "Escalations",
      icon: AlertTriangle,
      badge: openInterrupts,
      badgeColor: "rose",
    },
    {
      id: "integrations",
      label: "Integrations",
      icon: KeyRound,
      badge: alerts.length,
      // Sky, not rose -- an open alert here is informational (a quota was hit),
      // never a decision the factory is blocked waiting on, so it must never
      // read with the same urgency as an Escalations badge.
      badgeColor: "sky",
    },
  ];

  async function handleLogout() {
    await fetch("/api/auth/logout", { method: "POST" });
    router.push("/login");
    router.refresh();
  }

  return (
    <aside className="w-64 h-full bg-slate-900 border-r border-slate-800 flex flex-col text-slate-300">
      <div className="p-6 border-b border-slate-800 bg-slate-950">
        <h1 className="text-xl font-bold tracking-tight text-white flex items-center gap-2">
          <div
            className={`w-3 h-3 rounded-full ${
              health?.db_connected ? "bg-emerald-500 animate-pulse" : "bg-rose-500"
            }`}
          />
          Factory OS
        </h1>
        <p className="text-xs text-slate-500 mt-1 font-mono">
          sys.status: {health?.db_connected ? "ONLINE" : "DB UNREACHABLE"}
        </p>
      </div>
      <nav className="flex-1 p-4 space-y-1">
        {tabs.map((tab) => {
          const Icon = tab.icon;
          const isActive = activeTab === tab.id;
          return (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              className={`w-full flex items-center justify-between px-3 py-2.5 rounded-md text-sm font-medium transition-colors ${
                isActive
                  ? "bg-indigo-500/10 text-indigo-400 border border-indigo-500/20"
                  : "text-slate-400 hover:bg-slate-800 hover:text-slate-200 border border-transparent"
              }`}
            >
              <div className="flex items-center gap-3">
                <Icon className={`w-4 h-4 ${isActive ? "text-indigo-400" : "text-slate-500"}`} />
                {tab.label}
              </div>
              {!!tab.badge && (
                <span
                  className={`flex h-5 w-5 items-center justify-center rounded-full text-[10px] font-bold border ${
                    "badgeColor" in tab && tab.badgeColor === "sky"
                      ? "bg-sky-500/20 text-sky-400 border-sky-500/20"
                      : "bg-rose-500/20 text-rose-400 border-rose-500/20"
                  }`}
                >
                  {tab.badge}
                </span>
              )}
            </button>
          );
        })}
      </nav>
      <div className="p-4 border-t border-slate-800 bg-slate-950 space-y-3">
        <div className="text-[10px] font-mono text-slate-600 uppercase tracking-wider space-y-1">
          <div>Mode: {health?.mode ?? "unknown"}</div>
          <div>DB: {health?.db_connected ? "Connected (Postgres)" : "Unreachable"}</div>
        </div>
        <button
          onClick={handleLogout}
          className="flex items-center gap-2 text-xs text-slate-500 hover:text-slate-300 transition-colors"
        >
          <LogOut className="w-3.5 h-3.5" />
          Sign out
        </button>
      </div>
    </aside>
  );
}
