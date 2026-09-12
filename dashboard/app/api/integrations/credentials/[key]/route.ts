import { NextRequest, NextResponse } from "next/server";
import { writeCredential } from "@/lib/factory-api";

// Same-origin, cookie-protected by middleware.ts. The one write path this
// page has: persists a new value to the real .env file the factory service
// reads at container (re)creation -- api/dashboard.py::write_credential is
// explicit that this never takes effect on the already-running process.
export async function POST(request: NextRequest, context: { params: Promise<{ key: string }> }) {
  const { key } = await context.params;
  const body = await request.json().catch(() => null);
  const value = typeof body?.value === "string" ? body.value.trim() : "";
  if (!value) {
    return NextResponse.json({ detail: "value must not be empty" }, { status: 400 });
  }

  try {
    const { status, body: factoryBody } = await writeCredential(key, value);
    return NextResponse.json(factoryBody, { status });
  } catch (error) {
    return NextResponse.json(
      { detail: error instanceof Error ? error.message : "unknown error" },
      { status: 502 }
    );
  }
}
