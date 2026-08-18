FROM oven/bun:1-alpine AS deps
WORKDIR /app
COPY package.json bun.lock ./
RUN bun install --frozen-lockfile

FROM oven/bun:1-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN bun run build

# next.config.ts sets output: 'standalone', so the runtime image only needs
# .next/standalone (a self-contained node_modules subset + server.js) and
# .next/static -- not the full builder stage or bun itself.
FROM node:20-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production

RUN addgroup --system --gid 1001 nodejs \
    && adduser --system --uid 1001 nextjs

COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

USER nextjs
EXPOSE 3000
ENV PORT=3000
# Next.js standalone's server.js binds to whatever HOSTNAME resolves to at
# startup, which without this defaulted to the container's own interface IP
# (172.18.0.x), never loopback -- real traffic through Caddy/compose DNS
# worked fine, but the healthcheck's own probe below always got
# connection-refused, reporting "unhealthy" despite the app genuinely working.
ENV HOSTNAME=0.0.0.0
# 127.0.0.1, not localhost: wget resolves "localhost" to ::1 first, and a
# 0.0.0.0 bind is IPv4-only -- the IPv6 attempt was refused and never fell
# back, so even after fixing HOSTNAME above, the literal string "localhost"
# still failed this exact healthcheck every time.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -qO- http://127.0.0.1:3000/login >/dev/null 2>&1 || exit 1

CMD ["node", "server.js"]
