import "server-only";
import type { DashboardState } from "@/hooks/use-factory-stream";
import type { IntegrationsState } from "@/hooks/use-integrations";

/**
 * The only place this app ever holds DASHBOARD_AUTH_TOKEN or talks to the
 * real factory. `server-only` is an import guard, not a runtime check -- an
 * accidental `"use client"` import of this file fails the *build*, rather
 * than silently bundling the token into a page shipped to the browser.
 */

export async function fetchDashboardState(): Promise<DashboardState> {
  const baseUrl = process.env.FACTORY_API_BASE_URL;
  const token = process.env.DASHBOARD_AUTH_TOKEN;
  if (!baseUrl || !token) {
    throw new Error("FACTORY_API_BASE_URL or DASHBOARD_AUTH_TOKEN is not configured");
  }

  const response = await fetch(`${baseUrl}/dashboard/state`, {
    headers: { Authorization: `Bearer ${token}` },
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error(`factory API returned ${response.status}`);
  }
  return response.json();
}

/**
 * Answers an open interrupt exactly the way a Telegram reply would (same
 * `hitl.interrupts.resolve_reply` on the backend). Returns the factory API's
 * real status code and body rather than throwing, so the route handler can
 * pass through a 404 ("already resolved" / unknown id) as a real 404 instead
 * of flattening every failure into one generic error.
 */
export async function resolveInterrupt(
  interruptId: string,
  response: string
): Promise<{ status: number; body: unknown }> {
  const baseUrl = process.env.FACTORY_API_BASE_URL;
  const token = process.env.DASHBOARD_AUTH_TOKEN;
  if (!baseUrl || !token) {
    throw new Error("FACTORY_API_BASE_URL or DASHBOARD_AUTH_TOKEN is not configured");
  }

  const factoryResponse = await fetch(
    `${baseUrl}/dashboard/interrupts/${encodeURIComponent(interruptId)}/resolve`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ response }),
      cache: "no-store",
    }
  );
  const body = await factoryResponse.json().catch(() => ({}));
  return { status: factoryResponse.status, body };
}

/**
 * Credential catalog (configured/not, masked previews only) plus open
 * usage-limit alerts -- kept off `fetchDashboardState`'s payload on purpose,
 * see `api/dashboard.py::integrations_state`'s own docstring for why.
 */
export async function fetchIntegrationsState(): Promise<IntegrationsState> {
  const baseUrl = process.env.FACTORY_API_BASE_URL;
  const token = process.env.DASHBOARD_AUTH_TOKEN;
  if (!baseUrl || !token) {
    throw new Error("FACTORY_API_BASE_URL or DASHBOARD_AUTH_TOKEN is not configured");
  }

  const response = await fetch(`${baseUrl}/dashboard/integrations`, {
    headers: { Authorization: `Bearer ${token}` },
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error(`factory API returned ${response.status}`);
  }
  return response.json();
}

/**
 * Persists a new credential value to the real .env file the factory service
 * reads at container (re)creation -- never takes effect on the already-
 * running process. Returns the factory API's real status/body (never
 * throws on a 4xx) so the route handler can pass through e.g. a 404 for a
 * non-editable/unknown key.
 */
export async function writeCredential(
  key: string,
  value: string
): Promise<{ status: number; body: unknown }> {
  const baseUrl = process.env.FACTORY_API_BASE_URL;
  const token = process.env.DASHBOARD_AUTH_TOKEN;
  if (!baseUrl || !token) {
    throw new Error("FACTORY_API_BASE_URL or DASHBOARD_AUTH_TOKEN is not configured");
  }

  const factoryResponse = await fetch(
    `${baseUrl}/dashboard/integrations/credentials/${encodeURIComponent(key)}`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ value }),
      cache: "no-store",
    }
  );
  const body = await factoryResponse.json().catch(() => ({}));
  return { status: factoryResponse.status, body };
}

/**
 * The free-form counterpart to `writeCredential` -- registers and writes a
 * credential this factory has no catalog entry for yet. Same {status, body}
 * shape: the key can be rejected (already-catalogued, malformed, reserved),
 * and that 400 should reach the form as a real, specific error.
 */
export async function createCustomCredential(
  key: string,
  value: string
): Promise<{ status: number; body: unknown }> {
  const baseUrl = process.env.FACTORY_API_BASE_URL;
  const token = process.env.DASHBOARD_AUTH_TOKEN;
  if (!baseUrl || !token) {
    throw new Error("FACTORY_API_BASE_URL or DASHBOARD_AUTH_TOKEN is not configured");
  }

  const factoryResponse = await fetch(`${baseUrl}/dashboard/integrations/credentials`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ key, value }),
    cache: "no-store",
  });
  const body = await factoryResponse.json().catch(() => ({}));
  return { status: factoryResponse.status, body };
}

/**
 * A product's business plan, design brief (if generated yet), and live
 * link -- fetched on demand when an operator opens one product's card, not
 * part of the bulk dashboard poll. {status, body} rather than throwing: a
 * 404 (unknown product) should reach the modal as a real "not found" state.
 */
export async function fetchProductDetail(
  productId: string
): Promise<{ status: number; body: unknown }> {
  const baseUrl = process.env.FACTORY_API_BASE_URL;
  const token = process.env.DASHBOARD_AUTH_TOKEN;
  if (!baseUrl || !token) {
    throw new Error("FACTORY_API_BASE_URL or DASHBOARD_AUTH_TOKEN is not configured");
  }

  const factoryResponse = await fetch(
    `${baseUrl}/dashboard/products/${encodeURIComponent(productId)}`,
    { headers: { Authorization: `Bearer ${token}` }, cache: "no-store" }
  );
  const body = await factoryResponse.json().catch(() => ({}));
  return { status: factoryResponse.status, body };
}

/**
 * Writes intent only -- the factory's own tick loop generates the design
 * brief within one tick (<=15s), this endpoint never calls an LLM itself.
 * See `api/dashboard.py::generate_design_brief`'s own docstring.
 */
export async function generateDesignBrief(
  productId: string
): Promise<{ status: number; body: unknown }> {
  const baseUrl = process.env.FACTORY_API_BASE_URL;
  const token = process.env.DASHBOARD_AUTH_TOKEN;
  if (!baseUrl || !token) {
    throw new Error("FACTORY_API_BASE_URL or DASHBOARD_AUTH_TOKEN is not configured");
  }

  const factoryResponse = await fetch(
    `${baseUrl}/dashboard/products/${encodeURIComponent(productId)}/design-brief/generate`,
    { method: "POST", headers: { Authorization: `Bearer ${token}` }, cache: "no-store" }
  );
  const body = await factoryResponse.json().catch(() => ({}));
  return { status: factoryResponse.status, body };
}

/** Dismisses an open usage-limit alert -- purely informational, no reply text. */
export async function resolveIntegrationAlert(
  alertId: string
): Promise<{ status: number; body: unknown }> {
  const baseUrl = process.env.FACTORY_API_BASE_URL;
  const token = process.env.DASHBOARD_AUTH_TOKEN;
  if (!baseUrl || !token) {
    throw new Error("FACTORY_API_BASE_URL or DASHBOARD_AUTH_TOKEN is not configured");
  }

  const factoryResponse = await fetch(
    `${baseUrl}/dashboard/integrations/alerts/${encodeURIComponent(alertId)}/resolve`,
    {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
      cache: "no-store",
    }
  );
  const body = await factoryResponse.json().catch(() => ({}));
  return { status: factoryResponse.status, body };
}
