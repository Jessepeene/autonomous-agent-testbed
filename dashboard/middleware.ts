import { NextRequest, NextResponse } from "next/server";
import { SESSION_COOKIE, verifySession } from "@/lib/session";

/**
 * Gates everything except /login and /api/auth/* -- the only two routes that
 * must work for a signed-out visitor. Matches the FastAPI backend's own
 * fail-closed rule: no DASHBOARD_AUTH_TOKEN configured means nothing is
 * servable, not that auth is silently skipped.
 */
export const config = {
  matcher: ["/((?!login|api/auth|_next/static|_next/image|favicon.ico).*)"],
};

export async function middleware(request: NextRequest) {
  const token = process.env.DASHBOARD_AUTH_TOKEN;
  const isApiRoute = request.nextUrl.pathname.startsWith("/api/");

  if (!token) {
    const detail = "DASHBOARD_AUTH_TOKEN is not configured";
    return isApiRoute
      ? NextResponse.json({ detail }, { status: 503 })
      : new NextResponse(detail, { status: 503 });
  }

  const cookie = request.cookies.get(SESSION_COOKIE)?.value;
  const valid = await verifySession(token, cookie);
  if (valid) {
    return NextResponse.next();
  }

  // API routes get a plain 401 -- the polling hook checks for this and
  // redirects client-side. Page navigation gets a real server-side redirect,
  // since a raw 401 body is not what a browser tab should show.
  if (isApiRoute) {
    return NextResponse.json({ detail: "unauthorized" }, { status: 401 });
  }
  return NextResponse.redirect(new URL("/login", request.url));
}
