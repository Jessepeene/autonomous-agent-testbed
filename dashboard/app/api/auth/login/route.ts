import { timingSafeEqual } from "crypto";
import { NextRequest, NextResponse } from "next/server";
import { SESSION_COOKIE, SESSION_TTL_MS, signSession } from "@/lib/session";

// Node runtime, not Edge: needs Node's real `timingSafeEqual` for the
// password comparison, and reads DASHBOARD_AUTH_TOKEN server-side only --
// never NEXT_PUBLIC_*, so it can never end up in a client bundle.
export const runtime = "nodejs";

export async function POST(request: NextRequest) {
  const token = process.env.DASHBOARD_AUTH_TOKEN;
  if (!token) {
    return NextResponse.json(
      { detail: "DASHBOARD_AUTH_TOKEN is not configured" },
      { status: 503 }
    );
  }

  const body = await request.json().catch(() => null);
  const password = typeof body?.password === "string" ? body.password : "";

  const expected = Buffer.from(token);
  const presented = Buffer.from(password);
  // Length check first: timingSafeEqual throws (not returns false) on a
  // length mismatch, and the length check itself must run whether or not
  // the buffers match, or that becomes its own timing signal.
  const match = expected.length === presented.length && timingSafeEqual(expected, presented);
  if (!match) {
    return NextResponse.json({ detail: "incorrect password" }, { status: 401 });
  }

  const session = await signSession(token);
  const response = NextResponse.json({ ok: true });
  response.cookies.set(SESSION_COOKIE, session, {
    httpOnly: true,
    secure: true,
    sameSite: "lax",
    path: "/",
    maxAge: SESSION_TTL_MS / 1000,
  });
  return response;
}
