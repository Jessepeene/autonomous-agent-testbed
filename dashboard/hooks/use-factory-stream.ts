import { useEffect, useRef, useState } from "react";

/**
 * Real types, matching `observability/dashboard.py::DashboardSnapshot.as_dict()`
 * field-for-field (snake_case, exactly as the backend sends it -- no
 * camelCase transformation layer to keep in sync by hand). Every field here
 * is backed by a real column; nothing is invented to fill a gap the schema
 * can't back. See the dashboard-integration plan for the per-field reasoning
 * on what got dropped from the original mocked shape rather than kept.
 */

export type ProviderStatus = "up" | "degraded" | "down";

export interface ProviderHealth {
  status: ProviderStatus;
  errors_30m: number;
  calls_30m: number;
}

export interface SystemHealth {
  mode: "dry_run" | "active";
  db_connected: boolean;
  provider_health: Record<string, ProviderHealth>; // "anthropic" | "gemini"
  global_pause: boolean;
  sandbox_health: "clean" | "degraded";
  daily_llm_spend_cap_usd: number | null;
}

export interface FactoryMetrics {
  tasks_by_status: Record<string, number>;
  products_by_status: Record<string, number>;
  ideas_by_status: Record<string, number>;
  leads_total: number;
  outreach_sent: number;
  outreach_suppressed: number;
  open_interrupts: number;
  balance_cents: number;
  reinvest_pool_cents: number;
  llm_spend_usd_24h: number;
  mrr_cents: number;
  active_subscriptions: number;
  churn_rate_30d: number;
  involuntary_churn_30d: number;
  voluntary_churn_30d: number;
}

export interface MarketingSection {
  leads_total: number;
  outreach_sent: number;
  outreach_suppressed: number;
  deliverability_sent_30d: number;
  deliverability_complained_30d: number;
  deliverability_bounced_30d: number;
  freeze_active: boolean;
  freeze_reason: string | null;
  spam_complaint_rate_threshold: number;
  bounce_rate_threshold: number;
}

// Real ProductStatus values (state/schemas.py) -- not the mocked
// DISCOVERY/COMPLIANCE/VALIDATION/BUILD taxonomy. Discovery and Compliance
// are stages an *Idea* goes through, before a Product row exists at all.
export type ProductStage =
  | "validating"
  | "planned"
  | "building"
  | "reviewing"
  | "patching"
  | "live"
  | "retired";

export type EngineStatus = "idle" | "running" | "paused";

export interface Engine {
  name: string;
  status: EngineStatus;
  in_flight: number;
  max_concurrency: number | null; // null = no configured ceiling
  disabled_reason: string | null;
}

// Real TaskStatus values -- the mock's "BACKOFF" doesn't exist; a backing-off
// task is really "pending" with not_before in the future.
export type TaskState = "pending" | "claimed" | "running" | "done" | "failed" | "escalated";

export interface Task {
  id: string;
  kind: string;
  status: TaskState;
  engine: string | null;
  priority: number; // raw int, lower = more urgent (10 = finance, 100 default, 500 backlog)
  attempt_count: number;
  idempotency_key: string;
  updated_at: string;
}

export interface Product {
  id: string;
  name: string;
  niche: string;
  idea_score: number;
  status: ProductStage;
  heal_attempts: number;
  mrr_cents: number;
  updated_at: string;
}

export interface Interrupt {
  id: string;
  reason: string;
  subject: string;
  detail: string;
  headline: string | null;
  reply_hint: string | null;
  product_id: string | null;
  created_at: string;
}

// Real TunableParameter.status values -- "proposed" from the original mock
// maps to "candidate" here.
export type TrialStatus = "candidate" | "trial" | "active" | "retired";

export interface Trial {
  id: string;
  key: string;
  kind: string;
  status: TrialStatus;
  target_metric: string;
  numerator: number;
  denominator: number;
  baseline_numerator: number | null;
  baseline_denominator: number | null;
  p_value: number | null; // raw Fisher's-exact p-value, not a fabricated "confidence %"
  created_at: string;
}

export interface ActivityItem {
  id: string;
  level: "info" | "warn";
  source: "task" | "interrupt";
  message: string;
  created_at: string;
}

export interface DashboardState {
  health: SystemHealth;
  metrics: FactoryMetrics;
  marketing: MarketingSection;
  engines: Engine[];
  tasks: Task[];
  products: Product[];
  interrupts: Interrupt[];
  trials: Trial[];
  activity: ActivityItem[];
  balance_cents: number;
  reinvest_pool_cents: number;
  last_ledger_write: string | null;
  max_task_attempts: number;
  generated_at: string;
}

const POLL_INTERVAL_MS = 5000; // a real Postgres-backed endpoint, one operator's
// browser -- the mock's 450ms was tuned purely to make fabricated numbers look alive.

export function useFactoryStream() {
  const [state, setState] = useState<DashboardState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const cancelledRef = useRef(false);
  const pollRef = useRef<() => Promise<void>>(async () => {});

  useEffect(() => {
    cancelledRef.current = false;

    async function poll() {
      try {
        const response = await fetch("/api/dashboard", { cache: "no-store" });
        if (response.status === 401) {
          window.location.href = "/login";
          return;
        }
        if (!response.ok) {
          const body = await response.json().catch(() => ({}));
          throw new Error(typeof body.detail === "string" ? body.detail : `HTTP ${response.status}`);
        }
        const data: DashboardState = await response.json();
        if (!cancelledRef.current) {
          setState(data);
          setError(null);
        }
      } catch (err) {
        if (!cancelledRef.current) {
          setError(err instanceof Error ? err.message : "failed to reach the dashboard API");
        }
      }
    }

    pollRef.current = poll;
    poll();
    const interval = setInterval(poll, POLL_INTERVAL_MS);
    return () => {
      cancelledRef.current = true;
      clearInterval(interval);
    };
  }, []);

  return {
    state,
    loading: state === null && error === null,
    error,
    // Lets a write action (e.g. resolving an interrupt) pull the next state
    // in immediately instead of waiting up to POLL_INTERVAL_MS for it to
    // disappear from the list on its own.
    refresh: () => pollRef.current(),
    // Convenience destructuring so existing components can keep reading
    // e.g. `health`/`engines` directly rather than `state.health` everywhere.
    health: state?.health ?? null,
    metrics: state?.metrics ?? null,
    marketing: state?.marketing ?? null,
    engines: state?.engines ?? [],
    tasks: state?.tasks ?? [],
    products: state?.products ?? [],
    interrupts: state?.interrupts ?? [],
    trials: state?.trials ?? [],
    activity: state?.activity ?? [],
  };
}
