import { NextResponse } from "next/server";
import { resolveIntegrationAlert } from "@/lib/factory-api";

// Same-origin, cookie-protected by middleware.ts -- dismisses an open
// usage-limit alert. Purely informational, so unlike an interrupt reply
// there is no reply text to validate.
export async function POST(_request: Request, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;

  try {
    const { status, body } = await resolveIntegrationAlert(id);
    return NextResponse.json(body, { status });
  } catch (error) {
    return NextResponse.json(
      { detail: error instanceof Error ? error.message : "unknown error" },
      { status: 502 }
    );
  }
}
