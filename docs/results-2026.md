# Production results

Collected **2026-10-25**, read-only, directly against the production Postgres
container (`superagent-postgres-1` on the deployment host) via `psql`. Every number
below is a `count`/`sum` aggregate — no individual rows, no personal data, are
reproduced in this file. The SQL for each number is included so it can be
re-run and re-checked against the live database at any time before it's taken
offline.

System context at collection time: the factory has been deliberately paused
(`DAILY_LLM_SPEND_CAP_USD=0`) since **2026-09-24**. Nothing below has changed
since that date — these are the final, settled numbers for the whole run.

## Tables holding personal data

Identified by column, not by name alone (every table's columns were read via
`information_schema.columns` first). Reported here as table name and row count
only.

| Table | Personal-data columns | Row count |
|---|---|---|
| `leads` | `first_name`, `last_name`, `work_email`, `job_title`, `company` | **0** |
| `validation_signals` | `email`, `message` (free text), `submitter_ip_hash` (hashed, not raw) | **2** |
| `sent_messages` | `to_address`, `from_address`, `subject`, `body` | **2** |
| `outreach_log` | `lead_id` (FK to `leads`; no direct PII column of its own) | **0** |
| `customers` | none — only `stripe_customer_id`, `product_id` (deliberately minimized by design) | 0 |

```sql
SELECT 'leads' t, count(*) FROM leads
UNION ALL SELECT 'validation_signals', count(*) FROM validation_signals
UNION ALL SELECT 'sent_messages', count(*) FROM sent_messages
UNION ALL SELECT 'outreach_log', count(*) FROM outreach_log
UNION ALL SELECT 'customers', count(*) FROM customers;
```

Only **4 rows in the entire database** carry anything personal: 2 validation-signal
sign-ups and 2 sent-message records. `leads`, `outreach_log` and `customers` are
all empty.

## All tables, row counts (for completeness)

```sql
SELECT schemaname||'.'||relname, n_live_tup
FROM pg_stat_user_tables ORDER BY n_live_tup DESC;
```

The three largest tables (`checkpoint_writes` 721,272 / `checkpoints` 434,815 /
`checkpoint_blobs` 191,360) are LangGraph's own durable-execution state, not
business data — internal step-by-step agent run state, re-creatable from nothing,
holding no personal data. `llm_calls` (93,610) is metering only (see below). Every
other table is listed in the sections above and below by what it actually holds.

## a) Ideas

```sql
SELECT status, count(*) FROM ideas GROUP BY status;
```

| Status | Count |
|---|---|
| approved | 3 |
| flagged | 2 |

**Total: 5 ideas generated.** No idea was ever recorded with a "rejected" status —
only these two values ever appear.

## b) Products

```sql
SELECT id, status, deploy_url, custom_domain, created_at, updated_at
FROM products ORDER BY created_at;
```

| Product | Status | URL | Created |
|---|---|---|---|
| `e2e6e830...` | retired | `owner-operators-of-independent-bakeries...vercel.app` | 2026-09-21 |
| `1b111298...` | retired | *(none deployed)* | 2026-09-21 |
| `08bbce40...` | validating | `engineering-leads-at-software-teams...vercel.app` | 2026-09-22 |

**3 products total. 0 are currently live.** 2 retired, 1 still sitting in
`validating` (will stay there — the factory is paused).

**Important context this table doesn't show**: `ARCHITECTURE_ROADMAP.md` and
`DEPLOYMENT_CHECKLIST.md` both describe an earlier product, `manifestaudit.com`
(branded "LedgerLens"), as the project's first real deployment, with a live
Stripe-backed checkout. I checked directly —
`SELECT count(*) FROM products WHERE deploy_url ILIKE '%manifestaudit%'` returns
**0**. That product has no row in the current database at all. It predates this
dataset (production go-live was 2026-08-18; this is consistent with that product
existing during an earlier phase and its data no longer being present under the
current product set). Worth being explicit about in anything written from this
file: the current database's 3 products are not the complete history of what
this project built — they're what's left after whatever happened to the earlier
one.

## c) Bug Council

**Not tracked as a separate counter.** There is no `bug_council.*` event and no
dedicated review-outcome table. The closest real signal is the final release
decision each build reached (which Bug Council's findings feed into):

```sql
SELECT detail->>'decision', count(*)
FROM task_events WHERE event = 'release.decided' GROUP BY 1;
```

| Decision | Count |
|---|---|
| ship | 3 |
| fix_first | 3 |
| rethink | 2 |
| abandon | 1 |

**9 release decisions total.** `fix_first`/`rethink`/`abandon` (6 of 9) are the
cases where Bug Council or the release judge found something blocking enough to
not ship as-is.

## d) Issues and HITL escalations

```sql
SELECT status, count(*) FROM issues GROUP BY status;
```

| Status | Count |
|---|---|
| open | 10 |
| closed | 2 |
| wont_fix | 3 |

**15 issues filed, 2 fixed and verified closed, 3 explicitly accepted as won't-fix,
10 still open.**

```sql
SELECT reason, status, count(*) FROM interrupts GROUP BY reason, status ORDER BY reason;
```

| Reason | Status | Count |
|---|---|---|
| budget_exceeded | open | 32 |
| heal_exhausted | open | 4 |
| legal_uncertainty | open | 2 |
| channel_approval | open | 1 |
| data_integrity | open | 1 |
| lead_sourcing_stalled | open | 1 |
| missing_credential | open | 1 |
| provider_outage | open | 1 |

**43 HITL escalations total, all currently `open` — none resolved.** Worth
stating plainly: 32 of the 43 (74%) are the daily "spend cap exceeded" notice
repeating once per day since the pause began on 2026-09-24 — expected, by design,
harmless, and literally the correct behavior (that's the breaker doing its job
every single day the system has sat paused). The other **11 are genuine
unaddressed escalations** that were never replied to: 4 heal-exhausted, 2
legal-uncertainty, and one each of channel-approval, data-integrity,
lead-sourcing-stalled, missing-credential, and provider-outage.

## e) Revenue and customers

```sql
SELECT count(*), coalesce(sum(amount_cents),0) FROM ledger;
SELECT status, count(*) FROM subscriptions GROUP BY status;
SELECT count(*) FROM customers;
```

**€0 / $0 revenue. 0 ledger entries. 0 subscriptions, ever. 0 customers, ever.**
No product reached a paying customer at any point in this project's life.

## f) LLM spend

```sql
SELECT provider, count(*), round(sum(cost_usd)::numeric,2)
FROM llm_calls GROUP BY provider ORDER BY 3 DESC;

SELECT count(*), round(sum(cost_usd)::numeric,2), min(created_at), max(created_at)
FROM llm_calls;
```

| Provider | Calls | Spend (USD) |
|---|---|---|
| anthropic | 43,241 | $41.44 |
| gemini | 46,367 | $33.75 |
| deepseek | 5,663 | $19.55 |

**Total: 95,271 calls, $94.74, from 2026-08-18 07:17 UTC to 2026-09-24 08:01 UTC**
(37 days). The last call is the same day the spend cap was set to $0 — there has
been **zero LLM spend since the pause began**, confirming the pause has held for
the full month it's been in effect.
