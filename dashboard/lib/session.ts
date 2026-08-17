/**
 * Stateless signed session, built on Web Crypto's SubtleCrypto rather than
 * Node's `crypto` module. This has to run identically in `middleware.ts`
 * (Edge runtime, no Node `crypto`) and in Route Handlers (Node runtime) --
 * SubtleCrypto is the one signing API both support, so one implementation
 * serves both call sites with zero new dependency.
 *
 * No session store: the cookie itself is `${expiresAtMs}.${hmacHex}`, keyed
 * off a value *derived* from DASHBOARD_AUTH_TOKEN (never the raw token used
 * as the MAC key directly) so a forged cookie is exactly as hard to produce
 * as guessing the real password.
 */

export const SESSION_COOKIE = "factory_dashboard_session";
export const SESSION_TTL_MS = 24 * 60 * 60 * 1000; // 24h

async function hmacKey(secret: string): Promise<CryptoKey> {
  const keyMaterial = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(`${secret}:session`)
  );
  return crypto.subtle.importKey(
    "raw",
    keyMaterial,
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign", "verify"]
  );
}

function toHex(buffer: ArrayBuffer): string {
  return Array.from(new Uint8Array(buffer))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

export async function signSession(
  secret: string,
  expiresAtMs: number = Date.now() + SESSION_TTL_MS
): Promise<string> {
  const key = await hmacKey(secret);
  const payload = String(expiresAtMs);
  const signature = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(payload));
  return `${payload}.${toHex(signature)}`;
}

export async function verifySession(
  secret: string,
  cookieValue: string | undefined
): Promise<boolean> {
  if (!cookieValue) return false;
  const [payload, signatureHex] = cookieValue.split(".");
  if (!payload || !signatureHex) return false;

  const expiresAtMs = Number(payload);
  if (!Number.isFinite(expiresAtMs) || Date.now() > expiresAtMs) return false;

  const key = await hmacKey(secret);
  const expectedSignature = await crypto.subtle.sign(
    "HMAC",
    key,
    new TextEncoder().encode(payload)
  );
  const expectedHex = toHex(expectedSignature);

  // SubtleCrypto has no built-in timing-safe string compare, so this does it
  // by hand: fixed-length XOR over every character rather than a `===` that
  // could short-circuit on the first mismatched byte.
  if (expectedHex.length !== signatureHex.length) return false;
  let diff = 0;
  for (let i = 0; i < expectedHex.length; i++) {
    diff |= expectedHex.charCodeAt(i) ^ signatureHex.charCodeAt(i);
  }
  return diff === 0;
}
