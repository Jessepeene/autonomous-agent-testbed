"use client";

import { Sidebar } from "@/components/dashboard/Sidebar";
import { MainContent } from "@/components/dashboard/MainContent";
import { useState } from "react";
import { Menu } from "lucide-react";

export default function Home() {
  return (
    <div className="flex flex-col md:flex-row h-screen bg-slate-50 text-slate-900 overflow-hidden font-sans relative">
      <MainLayout />
    </div>
  );
}

function MainLayout() {
  const [activeTab, setActiveTab] = useState("health");
  const [isSidebarOpen, setIsSidebarOpen] = useState(false);

  return (
    <>
      {/* Mobile Header */}
      <header className="md:hidden flex items-center justify-between bg-slate-950 p-4 border-b border-slate-800 text-white z-20">
        <h1 className="text-lg font-bold tracking-tight flex items-center gap-2">
          <div className="w-2.5 h-2.5 rounded-full bg-emerald-500 animate-pulse" />
          Factory OS
        </h1>
        <button onClick={() => setIsSidebarOpen(true)} className="p-2 -mr-2 text-slate-400 hover:text-white">
          <Menu className="w-6 h-6" />
        </button>
      </header>

      {/* Sidebar overlay for mobile */}
      {isSidebarOpen && (
        <div 
          className="fixed inset-0 bg-black/60 z-30 md:hidden" 
          onClick={() => setIsSidebarOpen(false)}
        />
      )}

      {/* Sidebar container */}
      <div className={`fixed inset-y-0 left-0 transform ${isSidebarOpen ? "translate-x-0" : "-translate-x-full"} md:relative md:translate-x-0 transition duration-200 ease-in-out z-40 md:z-auto h-full`}>
        <Sidebar activeTab={activeTab} setActiveTab={(t) => {
          setActiveTab(t);
          setIsSidebarOpen(false); // Close on mobile when selecting a tab
        }} />
      </div>

      <main className="flex-1 overflow-y-auto overflow-x-hidden bg-slate-50 h-full relative z-10">
        <MainContent activeTab={activeTab} />
      </main>
    </>
  );
}
