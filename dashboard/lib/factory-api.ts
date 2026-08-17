import "server-only";
import type { DashboardState } from "@/hooks/use-factory-stream";

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
