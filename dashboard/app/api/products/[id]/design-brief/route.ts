import { NextResponse } from "next/server";
import { generateDesignBrief } from "@/lib/factory-api";

// Same-origin, cookie-protected by middleware.ts. Writes intent only -- the
// factory's own tick loop does the real generation; see
// api/dashboard.py::generate_design_brief's own docstring for why this
// route (and the endpoint it proxies to) never calls an LLM itself.
export async function POST(_request: Request, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;

  try {
    const { status, body } = await generateDesignBrief(id);
    return NextResponse.json(body, { status });
  } catch (error) {
    return NextResponse.json(
      { detail: error instanceof Error ? error.message : "unknown error" },
      { status: 502 }
    );
  }
}
