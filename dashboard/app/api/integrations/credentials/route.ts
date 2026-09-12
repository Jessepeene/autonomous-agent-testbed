import { NextRequest, NextResponse } from "next/server";
import { createCustomCredential } from "@/lib/factory-api";

// Same-origin, cookie-protected by middleware.ts. The free-form counterpart
// to app/api/integrations/credentials/[key]/route.ts -- the key comes from
// the request body here instead of the URL, since it isn't a pre-known one.
export async function POST(request: NextRequest) {
  const body = await request.json().catch(() => null);
  const key = typeof body?.key === "string" ? body.key.trim() : "";
  const value = typeof body?.value === "string" ? body.value.trim() : "";
  if (!key) {
    return NextResponse.json({ detail: "key must not be empty" }, { status: 400 });
  }
  if (!value) {
    return NextResponse.json({ detail: "value must not be empty" }, { status: 400 });
  }

  try {
    const { status, body: factoryBody } = await createCustomCredential(key, value);
    return NextResponse.json(factoryBody, { status });
  } catch (error) {
    return NextResponse.json(
      { detail: error instanceof Error ? error.message : "unknown error" },
      { status: 502 }
    );
  }
}
