"use client";

import { useMemo, useState } from "react";
import { useIntegrations } from "@/hooks/use-integrations";
import type { CredentialStatus, IntegrationAlert } from "@/hooks/use-integrations";
import { CheckCircle2, KeyRound, Lock, Pencil, Plus, Send, X, Zap } from "lucide-react";
import { motion } from "motion/react";

function timeAgo(iso: string): string {
  const min = Math.floor((Date.now() - new Date(iso).getTime()) / 60000);
  if (min < 1) return "just now";
  if (min < 60) return `${min}m ago`;
  const hrs = Math.floor(min / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

export function Integrations() {
  const { credentials, alerts, envWriteConfigured, loading, error, refresh } = useIntegrations();
  const [savedBanner, setSavedBanner] = useState(false);

  const grouped = useMemo(() => {
    const byGroup = new Map<string, CredentialStatus[]>();
    for (const cred of credentials) {
      const list = byGroup.get(cred.group) ?? [];
      list.push(cred);
      byGroup.set(cred.group, list);
    }
    return Array.from(byGroup.entries());
  }, [credentials]);

  if (loading) {
    return <div className="text-sm text-slate-500">Loading integrations...</div>;
  }

  return (
    <div className="space-y-6 max-w-4xl">
      <div>
        <h2 className="text-2xl font-bold tracking-tight text-slate-900">Integrations</h2>
        <p className="text-sm text-slate-500 mt-1">
          Every third-party credential the factory knows how to use, and usage-limit alerts from
          those providers — kept separate from Escalations, since nothing here needs a decision,
          only visibility.
        </p>
      </div>

      {error && (
        <div className="p-3 bg-rose-50 border border-rose-200 rounded-lg text-xs text-rose-700">
          {error}
        </div>
      )}

      {savedBanner && (
        <div className="flex items-center justify-between gap-3 p-4 bg-amber-50 border border-amber-300 rounded-xl text-amber-900">
          <div className="flex items-center gap-3">
            <Zap className="w-5 h-5 shrink-0 text-amber-500" />
            <div className="text-sm">
              <span className="font-bold">Saved to .env.</span> This has no effect on the running
              factory yet — restart it (<code className="font-mono text-xs">docker compose up -d
              factory</code>) to pick up the new value.
            </div>
          </div>
          <button
            onClick={() => setSavedBanner(false)}
            className="text-amber-500 hover:text-amber-700 shrink-0"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
      )}

      <section className="space-y-3">
        <h3 className="font-bold text-slate-800 flex items-center gap-2">
          <Zap className="w-4 h-4 text-sky-600" />
          Usage-limit alerts {alerts.length > 0 && `(${alerts.length})`}
        </h3>
        {alerts.length === 0 ? (
          <div className="bg-slate-50 border border-slate-200 rounded-xl p-4 text-sm text-slate-500">
            No open alerts. Nothing has hit a quota or rate limit recently.
          </div>
        ) : (
          <div className="space-y-3">
            {alerts.map((alert) => (
              <AlertCard key={alert.id} alert={alert} onResolved={refresh} />
            ))}
          </div>
        )}
      </section>

      <section className="space-y-4">
        <h3 className="font-bold text-slate-800 flex items-center gap-2">
          <KeyRound className="w-4 h-4 text-indigo-600" />
          Credentials
        </h3>
        {!envWriteConfigured && (
          <div className="p-3 bg-slate-50 border border-slate-200 rounded-lg text-xs text-slate-500">
            ENV_FILE_PATH is not configured on the factory — credentials below are shown read-only
            until it is set.
          </div>
        )}
        <div className="space-y-6">
          {grouped.map(([group, creds]) => (
            <div key={group} className="bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden">
              <div className="px-4 py-2.5 bg-slate-50 border-b border-slate-100 text-xs font-semibold uppercase tracking-wide text-slate-500">
                {group}
              </div>
              <div className="divide-y divide-slate-100">
                {creds.map((cred) => (
                  <CredentialRow
                    key={cred.key}
                    credential={cred}
                    canWrite={envWriteConfigured}
                    onSaved={() => {
                      setSavedBanner(true);
                      refresh();
                    }}
                  />
                ))}
              </div>
            </div>
          ))}
        </div>

        {envWriteConfigured && (
          <AddCustomCredentialForm
            onSaved={() => {
              setSavedBanner(true);
              refresh();
            }}
          />
        )}
      </section>
    </div>
  );
}

function AddCustomCredentialForm({ onSaved }: { onSaved: () => void }) {
  const [open, setOpen] = useState(false);
  const [key, setKey] = useState("");
  const [value, setValue] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function save() {
    if (!key.trim() || !value.trim() || saving) return;
    setSaving(true);
    setError(null);
    try {
      const res = await fetch("/api/integrations/credentials", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: key.trim(), value: value.trim() }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`);
      }
      setKey("");
      setValue("");
      setOpen(false);
      onSaved();
    } catch (err) {
      setError(err instanceof Error ? err.message : "failed to save");
    } finally {
      setSaving(false);
    }
  }

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="flex items-center gap-1.5 px-3 py-2 text-xs font-semibold text-indigo-700 bg-indigo-50 hover:bg-indigo-100 rounded-lg transition-colors"
      >
        <Plus className="w-3.5 h-3.5" />
        Add a credential not listed above
      </button>
    );
  }

  return (
    <div className="bg-white rounded-xl border border-slate-200 shadow-sm p-4">
      <h4 className="text-sm font-semibold text-slate-800 mb-1">Add a new credential</h4>
      <p className="text-xs text-slate-500 mb-3">
        For a key this factory doesn&rsquo;t have a catalog entry for yet -- e.g. pre-staging a
        credential before its adapter code exists.
      </p>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          save();
        }}
        className="space-y-2"
      >
        <input
          autoFocus
          type="text"
          value={key}
          onChange={(event) => setKey(event.target.value)}
          placeholder="KEY_NAME (e.g. MY_NEW_API_KEY)"
          disabled={saving}
          className="w-full text-sm font-mono rounded-lg border border-slate-200 px-3 py-2 outline-none focus:border-indigo-400 disabled:opacity-50"
        />
        <input
          type="password"
          value={value}
          onChange={(event) => setValue(event.target.value)}
          placeholder="Value"
          disabled={saving}
          className="w-full text-sm rounded-lg border border-slate-200 px-3 py-2 outline-none focus:border-indigo-400 disabled:opacity-50"
        />
        <div className="flex items-center gap-2">
          <button
            type="submit"
            disabled={saving || !key.trim() || !value.trim()}
            className="px-3 py-2 bg-indigo-600 hover:bg-indigo-700 text-white text-xs font-semibold rounded-lg flex items-center gap-1.5 disabled:opacity-50"
          >
            <Send className="w-3.5 h-3.5" />
            {saving ? "Saving..." : "Save"}
          </button>
          <button
            type="button"
            onClick={() => {
              setOpen(false);
              setKey("");
              setValue("");
              setError(null);
            }}
            disabled={saving}
            className="px-2 py-2 text-slate-400 hover:text-slate-600"
          >
            <X className="w-3.5 h-3.5" />
          </button>
        </div>
      </form>
      {error && <p className="text-xs text-rose-600 mt-2">{error}</p>}
    </div>
  );
}

function AlertCard({ alert, onResolved }: { alert: IntegrationAlert; onResolved: () => void }) {
  const [resolving, setResolving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function resolve() {
    setResolving(true);
    setError(null);
    try {
      const res = await fetch(`/api/integrations/alerts/${encodeURIComponent(alert.id)}/resolve`, {
        method: "POST",
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`);
      }
      onResolved();
    } catch (err) {
      setError(err instanceof Error ? err.message : "failed to dismiss");
      setResolving(false);
    }
  }

  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      className="flex items-start justify-between gap-4 bg-sky-50 border border-sky-200 rounded-xl p-4"
    >
      <div className="flex items-start gap-3">
        <Zap className="w-4 h-4 text-sky-500 mt-0.5 shrink-0" />
        <div>
          <div className="text-sm font-semibold text-sky-900">{alert.provider}</div>
          <p className="text-xs text-sky-800 mt-0.5">{alert.message}</p>
          <div className="text-[10px] text-sky-600 font-mono mt-1" suppressHydrationWarning>
            {timeAgo(alert.created_at)}
          </div>
          {error && <p className="text-xs text-rose-600 mt-1">{error}</p>}
        </div>
      </div>
      <button
        onClick={resolve}
        disabled={resolving}
        className="text-xs font-semibold text-sky-700 hover:text-sky-900 disabled:opacity-50 shrink-0"
      >
        {resolving ? "Dismissing..." : "Dismiss"}
      </button>
    </motion.div>
  );
}

function CredentialRow({
  credential,
  canWrite,
  onSaved,
}: {
  credential: CredentialStatus;
  canWrite: boolean;
  onSaved: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function save() {
    if (!value.trim() || saving) return;
    setSaving(true);
    setError(null);
    try {
      const res = await fetch(`/api/integrations/credentials/${encodeURIComponent(credential.key)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ value: value.trim() }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`);
      }
      setValue("");
      setEditing(false);
      onSaved();
    } catch (err) {
      setError(err instanceof Error ? err.message : "failed to save");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="p-4">
      <div className="flex items-center justify-between gap-4">
        <div className="min-w-0">
          <div className="text-sm font-medium text-slate-800 flex items-center gap-2">
            {credential.label}
            {!credential.editable && <Lock className="w-3 h-3 text-slate-400" />}
          </div>
          <div className="text-xs font-mono mt-0.5 truncate">
            {credential.configured ? (
              <span className="text-emerald-600 flex items-center gap-1">
                <CheckCircle2 className="w-3 h-3 shrink-0" />
                {credential.preview}
              </span>
            ) : (
              <span className="text-slate-400">Not configured</span>
            )}
          </div>
        </div>
        {credential.editable && canWrite && !editing && (
          <button
            onClick={() => setEditing(true)}
            className="shrink-0 flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold text-indigo-700 bg-indigo-50 hover:bg-indigo-100 rounded-lg transition-colors"
          >
            <Pencil className="w-3.5 h-3.5" />
            {credential.configured ? "Update" : "Set"}
          </button>
        )}
      </div>

      {editing && (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            save();
          }}
          className="flex items-center gap-2 mt-3"
        >
          <input
            autoFocus
            type={credential.is_secret ? "password" : "text"}
            value={value}
            onChange={(event) => setValue(event.target.value)}
            placeholder={`New value for ${credential.label}`}
            disabled={saving}
            className="flex-1 text-sm rounded-lg border border-slate-200 px-3 py-2 outline-none focus:border-indigo-400 disabled:opacity-50"
          />
          <button
            type="submit"
            disabled={saving || !value.trim()}
            className="px-3 py-2 bg-indigo-600 hover:bg-indigo-700 text-white text-xs font-semibold rounded-lg flex items-center gap-1.5 disabled:opacity-50 shrink-0"
          >
            <Send className="w-3.5 h-3.5" />
            {saving ? "Saving..." : "Save"}
          </button>
          <button
            type="button"
            onClick={() => {
              setEditing(false);
              setValue("");
              setError(null);
            }}
            disabled={saving}
            className="px-2 py-2 text-slate-400 hover:text-slate-600 shrink-0"
          >
            <X className="w-3.5 h-3.5" />
          </button>
        </form>
      )}
      {error && <p className="text-xs text-rose-600 mt-2">{error}</p>}
    </div>
  );
}
