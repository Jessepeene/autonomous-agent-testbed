import { NextResponse } from "next/server";
import { fetchProductDetail } from "@/lib/factory-api";

// Same-origin, cookie-protected by middleware.ts. Fetched on demand when an
// operator opens one product's card -- not part of the bulk dashboard poll.
export async function GET(_request: Request, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;

  try {
    const { status, body } = await fetchProductDetail(id);
    return NextResponse.json(body, { status });
  } catch (error) {
    return NextResponse.json(
      { detail: error instanceof Error ? error.message : "unknown error" },
      { status: 502 }
    );
  }
}
