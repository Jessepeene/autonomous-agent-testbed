import { NextRequest, NextResponse } from "next/server";
import { resolveInterrupt } from "@/lib/factory-api";

// Same-origin, cookie-protected by middleware.ts -- the browser never sees
// FACTORY_API_BASE_URL or DASHBOARD_AUTH_TOKEN, only this route's JSON. The
// one write path this dashboard has: answers an open interrupt exactly the
// way a Telegram reply would (api/dashboard.py::resolve_interrupt).
export async function POST(request: NextRequest, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  const body = await request.json().catch(() => null);
  const response = typeof body?.response === "string" ? body.response.trim() : "";
  if (!response) {
    return NextResponse.json({ detail: "response must not be empty" }, { status: 400 });
  }

  try {
    const { status, body: factoryBody } = await resolveInterrupt(id, response);
    return NextResponse.json(factoryBody, { status });
  } catch (error) {
    return NextResponse.json(
      { detail: error instanceof Error ? error.message : "unknown error" },
      { status: 502 }
    );
  }
}
