"use client";

import { useEffect, useRef, useState } from "react";

/**
 * Real types, matching `observability/integrations.py::IntegrationsSnapshot
 * .as_dict()` field-for-field. Kept as a separate poll from
 * `useFactoryStream` on purpose: a credential catalog and its usage-limit
 * alerts change far less often than task/product state, so this has no
 * reason to round-trip on the main dashboard's 5s cadence.
 */

export interface CredentialStatus {
  key: string;
  label: string;
  group: string;
  is_secret: boolean;
  editable: boolean;
  configured: boolean;
  preview: string | null; // masked for secrets, plain for non-secrets, null if unconfigured
}

export interface IntegrationAlert {
  id: string;
  provider: string;
  message: string;
  created_at: string;
}

export interface IntegrationsState {
  credentials: CredentialStatus[];
  alerts: IntegrationAlert[];
  env_write_configured: boolean;
  generated_at: string;
}

const POLL_INTERVAL_MS = 15000;

export function useIntegrations() {
  const [state, setState] = useState<IntegrationsState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const cancelledRef = useRef(false);
  const pollRef = useRef<() => Promise<void>>(async () => {});

  useEffect(() => {
    cancelledRef.current = false;

    async function poll() {
      try {
        const response = await fetch("/api/integrations", { cache: "no-store" });
        if (response.status === 401) {
          window.location.href = "/login";
          return;
        }
        if (!response.ok) {
          const body = await response.json().catch(() => ({}));
          throw new Error(typeof body.detail === "string" ? body.detail : `HTTP ${response.status}`);
        }
        const data: IntegrationsState = await response.json();
        if (!cancelledRef.current) {
          setState(data);
          setError(null);
        }
      } catch (err) {
        if (!cancelledRef.current) {
          setError(err instanceof Error ? err.message : "failed to reach the integrations API");
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
    refresh: () => pollRef.current(),
    credentials: state?.credentials ?? [],
    alerts: state?.alerts ?? [],
    envWriteConfigured: state?.env_write_configured ?? false,
  };
}
