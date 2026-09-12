import { NextResponse } from "next/server";
import { fetchIntegrationsState } from "@/lib/factory-api";

// Same-origin, cookie-protected by middleware.ts -- the browser never sees
// FACTORY_API_BASE_URL or DASHBOARD_AUTH_TOKEN, only this route's JSON.
export async function GET() {
  try {
    const state = await fetchIntegrationsState();
    return NextResponse.json(state);
  } catch (error) {
    return NextResponse.json(
      { detail: error instanceof Error ? error.message : "unknown error" },
      { status: 502 }
    );
  }
}
