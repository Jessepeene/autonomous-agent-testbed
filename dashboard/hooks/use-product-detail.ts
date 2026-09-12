"use client";

import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Real types, matching `observability/dashboard.py::ProductDetail.as_dict()`
 * field-for-field. Fetched on demand when an operator opens one product's
 * card -- unlike `useFactoryStream`, this is not a continuous poll.
 */

export interface BusinessPlan {
  problem_statement: string;
  target_niche: string;
  monetization_hypothesis: string;
  self_serve_price_usd: number;
  score: number;
  evidence_count: number;
  source_url: string;
  pain_severity: number | null;
  willingness_to_pay: number | null;
  build_feasibility: number | null;
  adoption_ease: number | null;
  defensibility: number | null;
}

export interface DesignBrief {
  palette: string[];
  typography: string;
  tone: string;
}

export interface ProductDetail {
  id: string;
  name: string;
  status: string;
  deploy_url: string | null;
  heal_attempts: number;
  created_at: string;
  updated_at: string;
  business_plan: BusinessPlan;
  design_brief: DesignBrief | null;
}

// The tick loop picks up a queued GENERATE_DESIGN_BRIEF task within one tick
// (<=15s, see supervisor/scheduler.py) -- poll a bit past that rather than
// making the operator manually refresh to see the result land.
const GENERATE_POLL_INTERVAL_MS = 4000;
const GENERATE_POLL_TIMEOUT_MS = 30000;

export function useProductDetail(productId: string | null) {
  const [detail, setDetail] = useState<ProductDetail | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [generating, setGenerating] = useState(false);
  const pollTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const pollDeadlineRef = useRef(0);
  // Same pattern as useFactoryStream/useIntegrations: the fetch is defined
  // inline inside the effect (so a change in `productId` cleanly restarts
  // it), and stashed in a ref so `refresh()` and the generate-poll timer
  // below -- both outside the effect -- can still trigger it on demand.
  const loadRef = useRef<() => Promise<void>>(async () => {});

  useEffect(() => {
    async function load() {
      if (!productId) return;
      try {
        const response = await fetch(`/api/products/${encodeURIComponent(productId)}`, {
          cache: "no-store",
        });
        if (response.status === 401) {
          window.location.href = "/login";
          return;
        }
        if (response.status === 404) {
          setNotFound(true);
          setDetail(null);
          return;
        }
        if (!response.ok) {
          const body = await response.json().catch(() => ({}));
          throw new Error(
            typeof body.detail === "string" ? body.detail : `HTTP ${response.status}`
          );
        }
        const data: ProductDetail = await response.json();
        setNotFound(false);
        setError(null);
        setDetail(data);
        // Stop polling the moment a brief actually shows up, rather than
        // waiting out the full timeout window.
        if (data.design_brief && pollTimerRef.current) {
          clearInterval(pollTimerRef.current);
          pollTimerRef.current = null;
          setGenerating(false);
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : "failed to reach the products API");
      }
    }

    loadRef.current = load;
    load();
    return () => {
      if (pollTimerRef.current) clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    };
  }, [productId]);

  const generateDesignBrief = useCallback(async () => {
    if (!productId || generating) return;
    setGenerating(true);
    try {
      const response = await fetch(
        `/api/products/${encodeURIComponent(productId)}/design-brief`,
        { method: "POST" }
      );
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(
          typeof body.detail === "string" ? body.detail : `HTTP ${response.status}`
        );
      }
      pollDeadlineRef.current = Date.now() + GENERATE_POLL_TIMEOUT_MS;
      if (pollTimerRef.current) clearInterval(pollTimerRef.current);
      pollTimerRef.current = setInterval(() => {
        if (Date.now() >= pollDeadlineRef.current) {
          if (pollTimerRef.current) clearInterval(pollTimerRef.current);
          pollTimerRef.current = null;
          setGenerating(false);
          return;
        }
        loadRef.current();
      }, GENERATE_POLL_INTERVAL_MS);
    } catch (err) {
      setError(err instanceof Error ? err.message : "failed to request a design brief");
      setGenerating(false);
    }
  }, [productId, generating]);

  const loading = detail === null && !notFound && error === null;

  return {
    detail,
    loading,
    notFound,
    error,
    generating,
    generateDesignBrief,
    refresh: () => loadRef.current(),
  };
}
