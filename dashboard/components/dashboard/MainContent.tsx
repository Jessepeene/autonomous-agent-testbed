import { SystemHealthView } from "./views/SystemHealthView";
import { OrchestrationQueue } from "./views/OrchestrationQueue";
import { PortfolioPipeline } from "./views/PortfolioPipeline";
import { FinanceSpend } from "./views/FinanceSpend";
import { GrowthOutreach } from "./views/GrowthOutreach";
import { LearningLoop } from "./views/LearningLoop";
import { Interrupts } from "./views/Interrupts";

interface MainContentProps {
  activeTab: string;
}

export function MainContent({ activeTab }: MainContentProps) {
  return (
    <div className="p-3 md:p-6 w-full mx-auto min-h-screen">
      {activeTab === "health" && <SystemHealthView />}
      {activeTab === "orchestration" && <OrchestrationQueue />}
      {activeTab === "pipeline" && <PortfolioPipeline />}
      {activeTab === "finance" && <FinanceSpend />}
      {activeTab === "growth" && <GrowthOutreach />}
      {activeTab === "learning" && <LearningLoop />}
      {activeTab === "interrupts" && <Interrupts />}
    </div>
  );
}
