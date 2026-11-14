import React, { useCallback, useEffect, useState } from "react";
import {
  CreditCard,
  Database,
  FolderGit2,
  KeyRound,
  RefreshCw,
  Shield,
  Users,
} from "lucide-react";
import { apiRequest, randomKey } from "../api/client";
import type { ProjectItem, RepoItem } from "./TaskWorkbench";

interface SectionStatusProps {
  error: string;
  notice: string;
}

const StatusBanner: React.FC<SectionStatusProps> = ({ error, notice }) => (
  <>
    {error && <div className="alert alert-error" role="alert">{error}</div>}
    {notice && <div className="alert alert-ok">{notice}</div>}
  </>
);

/* ---------------- Projects, Templates & Repositories ---------------- */

interface MemoryItem {
  id: string;
  project_id: string;
  lesson_text: string;
  approved_by_user_id: string | null;
}

interface ProjectsSectionProps {
  projects: ProjectItem[];
  repos: RepoItem[];
  onRefreshCore: () => Promise<void>;
}

export const ProjectsSection: React.FC<ProjectsSectionProps> = ({ projects, repos, onRefreshCore }) => {
  const [projName, setProjName] = useState("");
  const [projDesc, setProjDesc] = useState("");
  const [templateProjId, setTemplateProjId] = useState("");
  const [templateId, setTemplateId] = useState("python-fastapi-starter");
  const [repoName, setRepoName] = useState("");
  const [memories, setMemories] = useState<MemoryItem[]>([]);
  const [lessonText, setLessonText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  useEffect(() => {
    if (!templateProjId && projects.length > 0) {
      setTemplateProjId(projects[0].id);
    }
  }, [projects, templateProjId]);

  const loadMemories = useCallback(async (pid: string) => {
    if (!pid) {
      setMemories([]);
      return;
    }
    try {
      const rows = await apiRequest<MemoryItem[]>(`/api/v1/projects/${pid}/memories`);
      setMemories(rows);
    } catch {
      setMemories([]);
    }
  }, []);

  useEffect(() => {
    void loadMemories(templateProjId);
  }, [templateProjId, loadMemories]);

  async function handleCreateProject(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setNotice("");
    setBusy(true);
    try {
      const created = await apiRequest<ProjectItem>("/api/v1/projects", {
        method: "POST",
        body: JSON.stringify({ name: projName, description: projDesc }),
      });
      setProjName("");
      setProjDesc("");
      setTemplateProjId(created.id);
      setNotice(`Created project ${created.name}.`);
      await onRefreshCore();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create project");
    } finally {
      setBusy(false);
    }
  }

  async function handleScaffoldTemplate(e: React.FormEvent) {
    e.preventDefault();
    if (!templateProjId) return;
    setError("");
    setNotice("");
    setBusy(true);
    try {
      const created = await apiRequest<RepoItem>("/api/v1/repos/from-template", {
        method: "POST",
        body: JSON.stringify({
          project_id: templateProjId,
          template_id: templateId,
          repo_name: repoName,
          run_baseline: false,
        }),
      });
      setRepoName("");
      setNotice(`Scaffolded repository ${created.full_name} (${created.support_profile}).`);
      await onRefreshCore();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Scaffold failed");
    } finally {
      setBusy(false);
    }
  }

  async function handleAddMemory(e: React.FormEvent) {
    e.preventDefault();
    if (!templateProjId) return;
    setError("");
    try {
      await apiRequest(`/api/v1/projects/${templateProjId}/memories`, {
        method: "POST",
        body: JSON.stringify({ lesson_text: lessonText, auto_approve: false }),
      });
      setLessonText("");
      await loadMemories(templateProjId);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save memory");
    }
  }

  return (
    <div>
      <StatusBanner error={error} notice={notice} />
      <div className="grid-cards">
        <div className="card">
          <h2 className="card-title">
            <span>
              <FolderGit2 size={16} /> Create Project
            </span>
          </h2>
          <form onSubmit={handleCreateProject}>
            <div className="field">
              <label>Project Name</label>
              <input
                className="input"
                required
                minLength={2}
                value={projName}
                onChange={(e) => setProjName(e.target.value)}
                placeholder="Core Platform"
                data-testid="input-project-name"
              />
            </div>
            <div className="field">
              <label>Description</label>
              <input
                className="input"
                value={projDesc}
                onChange={(e) => setProjDesc(e.target.value)}
                placeholder="Backend services and control plane"
              />
            </div>
            <button type="submit" className="btn btn-primary" disabled={busy} data-testid="btn-create-project">
              Create Project
            </button>
          </form>
        </div>

        <div className="card">
          <h2 className="card-title">Scaffold Template Repository</h2>
          <form onSubmit={handleScaffoldTemplate}>
            <div className="field">
              <label>Project</label>
              <select className="select" value={templateProjId} onChange={(e) => setTemplateProjId(e.target.value)}>
                {projects.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label>Starter Template</label>
              <select
                className="select"
                value={templateId}
                onChange={(e) => setTemplateId(e.target.value)}
                data-testid="select-template-id"
              >
                <option value="python-fastapi-starter">python-fastapi-starter</option>
                <option value="node-ts-vite-starter">node-ts-vite-starter</option>
              </select>
            </div>
            <div className="field">
              <label>Repository Name</label>
              <input
                className="input"
                required
                value={repoName}
                onChange={(e) => setRepoName(e.target.value)}
                placeholder="billing-service"
                data-testid="input-repo-name"
              />
            </div>
            <button type="submit" className="btn btn-primary" disabled={busy} data-testid="btn-scaffold-repo">
              Scaffold & Validate
            </button>
          </form>
        </div>
      </div>

      <div className="card">
        <h3 className="card-title">Connected Repositories ({repos.length})</h3>
        <table className="table">
          <thead>
            <tr>
              <th>Repository</th>
              <th>Profile</th>
              <th>Status</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {repos.map((r) => (
              <tr key={r.id}>
                <td className="mono">{r.full_name}</td>
                <td>{r.support_profile}</td>
                <td>
                  <span className="pill pill-success">{r.status}</span>
                </td>
                <td>
                  <button
                    type="button"
                    className="btn btn-sm"
                    onClick={() =>
                      void apiRequest(`/api/v1/repos/${r.id}/refresh`, { method: "POST" }).then(() => onRefreshCore())
                    }
                  >
                    <RefreshCw size={12} /> Refresh Snapshot
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="card">
        <h3 className="card-title">Project Memories (Human-Curated Guardrails)</h3>
        <form onSubmit={handleAddMemory} className="row" style={{ marginBottom: 10 }}>
          <input
            className="input"
            style={{ flex: 1 }}
            required
            minLength={5}
            value={lessonText}
            onChange={(e) => setLessonText(e.target.value)}
            placeholder="Add architectural lesson or guardrail note..."
          />
          <button type="submit" className="btn btn-sm btn-primary">
            Add Memory
          </button>
        </form>
        {memories.map((m) => (
          <div key={m.id} className="row" style={{ justifyContent: "space-between", padding: "6px 0" }}>
            <span>{m.lesson_text}</span>
            <div className="row">
              {!m.approved_by_user_id ? (
                <button
                  type="button"
                  className="btn btn-sm btn-success"
                  onClick={() =>
                    void apiRequest(`/api/v1/projects/${m.project_id}/memories/${m.id}/approve`, {
                      method: "POST",
                    }).then(() => loadMemories(m.project_id))
                  }
                >
                  Approve
                </button>
              ) : (
                <span className="pill pill-success">approved</span>
              )}
              <button
                type="button"
                className="btn btn-sm btn-danger"
                onClick={() =>
                  void apiRequest(`/api/v1/projects/${m.project_id}/memories/${m.id}`, {
                    method: "DELETE",
                  }).then(() => loadMemories(m.project_id))
                }
              >
                Delete
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
};

/* ---------------- Models, BYOK Credentials & Routing Policies ---------------- */

interface CredentialItem {
  id: string;
  name: string;
  provider_type: string;
  masked_preview: string;
  revoked_at: string | null;
}

interface RouteItem {
  id: string;
  name: string;
  provider: string;
  base_url: string;
  model_id: string;
  is_enabled: boolean;
  capabilities: Record<string, unknown>;
}

interface PolicyItem {
  id: string;
  project_id: string | null;
  compression_mode: string;
  per_task_budget_cents: number;
  policy_digest: string;
  role_route_map: Record<string, string>;
}

export const ModelsSection: React.FC = () => {
  const [creds, setCreds] = useState<CredentialItem[]>([]);
  const [routes, setRoutes] = useState<RouteItem[]>([]);
  const [policies, setPolicies] = useState<PolicyItem[]>([]);
  const [credName, setCredName] = useState("");
  const [credProvider, setCredProvider] = useState("openai_compatible");
  const [credSecret, setCredSecret] = useState("");
  const [routeName, setRouteName] = useState("");
  const [routeUrl, setRouteUrl] = useState("https://api.openai.com/v1");
  const [routeModel, setRouteModel] = useState("gpt-4o-mini");
  const [credRefId, setCredRefId] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const load = useCallback(async () => {
    try {
      const [c, r, p] = await Promise.all([
        apiRequest<CredentialItem[]>("/api/v1/credentials"),
        apiRequest<RouteItem[]>("/api/v1/models/routes"),
        apiRequest<PolicyItem[]>("/api/v1/models/policies"),
      ]);
      setCreds(c);
      setRoutes(r);
      setPolicies(p);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load model settings");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function handleCreateCred(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    try {
      const created = await apiRequest<CredentialItem>("/api/v1/credentials", {
        method: "POST",
        body: JSON.stringify({ name: credName, provider_type: credProvider, secret_value: credSecret }),
      });
      setCredName("");
      setCredSecret("");
      setCredRefId(created.id);
      setNotice(`Encrypted credential ${created.name} stored (${created.masked_preview}).`);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save credential");
    }
  }

  async function handleCreateRoute(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    try {
      const created = await apiRequest<RouteItem>("/api/v1/models/routes", {
        method: "POST",
        body: JSON.stringify({
          name: routeName,
          provider: credProvider === "9router" ? "9router" : "openai_compatible",
          base_url: routeUrl,
          model_id: routeModel,
          credential_ref_id: credRefId || null,
        }),
      });
      setRouteName("");
      setNotice(`Added model route ${created.name}.`);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to add model route");
    }
  }

  return (
    <div>
      <StatusBanner error={error} notice={notice} />
      <div className="grid-cards">
        <div className="card">
          <h3 className="card-title">
            <span>
              <KeyRound size={16} /> Store BYOK Credential
            </span>
          </h3>
          <form onSubmit={handleCreateCred}>
            <div className="field">
              <label>Name</label>
              <input className="input" required value={credName} onChange={(e) => setCredName(e.target.value)} />
            </div>
            <div className="field">
              <label>Provider Type</label>
              <select className="select" value={credProvider} onChange={(e) => setCredProvider(e.target.value)}>
                <option value="openai_compatible">openai_compatible</option>
                <option value="9router">9router</option>
                <option value="github_pat">github_pat</option>
              </select>
            </div>
            <div className="field">
              <label>Secret Key</label>
              <input
                className="input"
                type="password"
                required
                minLength={4}
                value={credSecret}
                onChange={(e) => setCredSecret(e.target.value)}
              />
            </div>
            <button type="submit" className="btn btn-primary">
              Encrypt & Store
            </button>
          </form>
        </div>

        <div className="card">
          <h3 className="card-title">Register Model Route</h3>
          <form onSubmit={handleCreateRoute}>
            <div className="field">
              <label>Route Name</label>
              <input className="input" required value={routeName} onChange={(e) => setRouteName(e.target.value)} />
            </div>
            <div className="field">
              <label>Base URL</label>
              <input className="input" required value={routeUrl} onChange={(e) => setRouteUrl(e.target.value)} />
            </div>
            <div className="field">
              <label>Model ID</label>
              <input className="input" required value={routeModel} onChange={(e) => setRouteModel(e.target.value)} />
            </div>
            <div className="field">
              <label>Credential (optional)</label>
              <select className="select" value={credRefId} onChange={(e) => setCredRefId(e.target.value)}>
                <option value="">None / Env Managed</option>
                {creds
                  .filter((c) => !c.revoked_at)
                  .map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name} ({c.masked_preview})
                    </option>
                  ))}
              </select>
            </div>
            <button type="submit" className="btn btn-primary">
              Create Route
            </button>
          </form>
        </div>
      </div>

      <div className="card">
        <h3 className="card-title">Credentials ({creds.length}) & Routes ({routes.length})</h3>
        {routes.map((r) => (
          <div key={r.id} className="row" style={{ justifyContent: "space-between", padding: "6px 0" }}>
            <span>
              <strong>{r.name}</strong> ({r.provider} • <span className="mono">{r.model_id}</span>)
            </span>
            <button
              type="button"
              className="btn btn-sm"
              onClick={() =>
                void apiRequest(`/api/v1/models/routes/${r.id}/probe`, { method: "POST" })
                  .then(() => load())
                  .catch((e) => setError(e instanceof Error ? e.message : "Probe failed"))
              }
            >
              Probe
            </button>
          </div>
        ))}
        <div style={{ marginTop: 10, fontSize: 12, color: "var(--text-muted)" }}>
          Active Routing Policies: {policies.length}
        </div>
      </div>
    </div>
  );
};

/* ---------------- Billing, Plans & Immutable Ledger ---------------- */

interface PlanItem {
  id: string;
  name: string;
  monthly_price_cents: number;
  included_credits_cents: number;
  max_concurrent_tasks: number;
}

interface SubscriptionInfo {
  plan_id: string;
  plan_name: string;
  status: string;
  credit_balance_cents: number;
  reserved_cents: number;
  available_cents: number;
}

interface LedgerItem {
  id: string;
  entry_type: string;
  amount_cents: number;
  description: string;
  created_at: string;
}

export const BillingSection: React.FC = () => {
  const [sub, setSub] = useState<SubscriptionInfo | null>(null);
  const [plans, setPlans] = useState<PlanItem[]>([]);
  const [ledger, setLedger] = useState<LedgerItem[]>([]);
  const [topupCents, setTopupCents] = useState("1000");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const load = useCallback(async () => {
    try {
      const [s, p, l] = await Promise.all([
        apiRequest<SubscriptionInfo>("/api/v1/billing/subscription"),
        apiRequest<PlanItem[]>("/api/v1/billing/plans"),
        apiRequest<LedgerItem[]>("/api/v1/billing/ledger"),
      ]);
      setSub(s);
      setPlans(p);
      setLedger(l);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load billing");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div>
      <StatusBanner error={error} notice={notice} />
      {sub && (
        <div className="card">
          <h3 className="card-title">
            <span>
              <CreditCard size={16} /> Subscription: {sub.plan_name} ({sub.status})
            </span>
            <span className="pill pill-info">Available: {sub.available_cents}¢</span>
          </h3>
          <div className="row" style={{ gap: 20 }}>
            <div>Balance: {sub.credit_balance_cents}¢</div>
            <div>Reserved: {sub.reserved_cents}¢</div>
            <div className="row">
              <input
                className="input"
                style={{ width: 120 }}
                type="number"
                min={100}
                value={topupCents}
                onChange={(e) => setTopupCents(e.target.value)}
              />
              <button
                type="button"
                className="btn btn-sm btn-primary"
                onClick={() =>
                  void apiRequest("/api/v1/billing/topup", {
                    method: "POST",
                    idempotencyKey: randomKey("topup"),
                    body: JSON.stringify({ amount_cents: Number(topupCents) || 500 }),
                  })
                    .then(() => {
                      setNotice("Credits topped up.");
                      return load();
                    })
                    .catch((e) => setError(e instanceof Error ? e.message : "Top-up failed"))
                }
              >
                Top Up (¢)
              </button>
            </div>
          </div>
        </div>
      )}

      <div className="grid-cards">
        {plans.map((pl) => (
          <div key={pl.id} className="card">
            <h4 className="card-title">
              <span>{pl.name}</span>
              <span>${(pl.monthly_price_cents / 100).toFixed(0)}/mo</span>
            </h4>
            <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 10 }}>
              {pl.included_credits_cents}¢ included • max {pl.max_concurrent_tasks} concurrent tasks
            </div>
            <button
              type="button"
              className="btn btn-sm btn-primary"
              disabled={sub?.plan_id === pl.id}
              onClick={() =>
                void apiRequest("/api/v1/billing/change-plan", {
                  method: "POST",
                  idempotencyKey: randomKey("plan"),
                  body: JSON.stringify({ plan_id: pl.id }),
                })
                  .then(() => {
                    setNotice(`Changed plan to ${pl.name}.`);
                    return load();
                  })
                  .catch((e) => setError(e instanceof Error ? e.message : "Plan change failed"))
              }
            >
              {sub?.plan_id === pl.id ? "Current Plan" : "Switch Plan"}
            </button>
          </div>
        ))}
      </div>

      <div className="card">
        <h3 className="card-title">Usage Ledger</h3>
        <table className="table">
          <thead>
            <tr>
              <th>Type</th>
              <th>Amount (¢)</th>
              <th>Description</th>
              <th>Created</th>
            </tr>
          </thead>
          <tbody>
            {ledger.slice(0, 20).map((e) => (
              <tr key={e.id}>
                <td>
                  <span className="pill">{e.entry_type}</span>
                </td>
                <td className="mono">{e.amount_cents}</td>
                <td>{e.description}</td>
                <td style={{ fontSize: 12, color: "var(--text-muted)" }}>{e.created_at}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
};

/* ---------------- Governance, Members, Exports, Support & Ops ---------------- */

interface GovernanceSectionProps {
  orgSlug: string;
  isPlatformOperator: boolean;
}

export const GovernanceSection: React.FC<GovernanceSectionProps> = ({ orgSlug, isPlatformOperator }) => {
  const [members, setMembers] = useState<Array<{ user_id: string; email: string; full_name: string; role: string }>>(
    [],
  );
  const [audits, setAudits] = useState<Array<{ id: string; action: string; target_type: string; created_at: string }>>(
    [],
  );
  const [exports, setExports] = useState<Array<{ id: string; status: string; created_at: string }>>([]);
  const [grants, setGrants] = useState<
    Array<{ id: string; operator_user_id: string; ticket_id: string; expires_at: string; revoked_at: string | null }>
  >([]);
  const [switches, setSwitches] = useState<
    Array<{ id: string; scope: string; target_key: string; is_active: boolean; reason: string }>
  >([]);
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteRole, setInviteRole] = useState("developer");
  const [lastInviteToken, setLastInviteToken] = useState("");
  const [acceptToken, setAcceptToken] = useState("");
  const [hardDeleteSlug, setHardDeleteSlug] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const load = useCallback(async () => {
    const [m, a, ex, gr] = await Promise.all([
      apiRequest<typeof members>("/api/v1/orgs/current/members").catch(() => []),
      apiRequest<typeof audits>("/api/v1/audit-events?limit=50").catch(() => []),
      apiRequest<typeof exports>("/api/v1/org/exports").catch(() => []),
      apiRequest<typeof grants>("/api/v1/support/grants").catch(() => []),
    ]);
    setMembers(m);
    setAudits(a);
    setExports(ex);
    setGrants(gr);
    if (isPlatformOperator) {
      const sw = await apiRequest<typeof switches>("/api/v1/ops/kill-switches").catch(() => []);
      setSwitches(sw);
    }
  }, [isPlatformOperator]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div>
      <StatusBanner error={error} notice={notice} />
      <div className="grid-cards">
        <div className="card">
          <h3 className="card-title">
            <span>
              <Users size={16} /> Team & Invitations ({members.length})
            </span>
          </h3>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              setError("");
              void apiRequest<{ invite_token?: string }>("/api/v1/orgs/current/invitations", {
                method: "POST",
                body: JSON.stringify({ email: inviteEmail, role: inviteRole }),
              })
                .then((res) => {
                  setInviteEmail("");
                  setLastInviteToken(res.invite_token || "");
                  setNotice("Invitation created.");
                  return load();
                })
                .catch((err) => setError(err instanceof Error ? err.message : "Invite failed"));
            }}
            className="row"
            style={{ marginBottom: 10 }}
          >
            <input
              className="input"
              style={{ flex: 1 }}
              type="email"
              required
              placeholder="teammate@acme.dev"
              value={inviteEmail}
              onChange={(e) => setInviteEmail(e.target.value)}
            />
            <select className="select" style={{ width: 120 }} value={inviteRole} onChange={(e) => setInviteRole(e.target.value)}>
              <option value="developer">developer</option>
              <option value="reviewer">reviewer</option>
              <option value="admin">admin</option>
            </select>
            <button type="submit" className="btn btn-sm btn-primary">
              Invite
            </button>
          </form>
          {lastInviteToken && (
            <div className="alert alert-ok mono" style={{ fontSize: 12 }}>
              Invite token: {lastInviteToken}
            </div>
          )}
          <div className="row" style={{ marginBottom: 10 }}>
            <input
              className="input mono"
              style={{ flex: 1 }}
              placeholder="Paste invite token to join another org..."
              value={acceptToken}
              onChange={(e) => setAcceptToken(e.target.value)}
            />
            <button
              type="button"
              className="btn btn-sm"
              onClick={() =>
                void apiRequest("/api/v1/orgs/invitations/accept", {
                  method: "POST",
                  body: JSON.stringify({ token: acceptToken }),
                })
                  .then(() => {
                    setAcceptToken("");
                    setNotice("Invitation accepted.");
                  })
                  .catch((err) => setError(err instanceof Error ? err.message : "Accept failed"))
              }
            >
              Accept
            </button>
          </div>
          {members.map((m) => (
            <div key={m.user_id} className="row" style={{ justifyContent: "space-between", padding: "4px 0" }}>
              <span>
                {m.full_name} ({m.email})
              </span>
              <span className="pill">{m.role}</span>
            </div>
          ))}
        </div>

        <div className="card">
          <h3 className="card-title">
            <span>
              <Database size={16} /> GDPR Export & Hard Delete
            </span>
          </h3>
          <button
            type="button"
            className="btn btn-sm btn-primary"
            style={{ marginBottom: 10 }}
            onClick={() =>
              void apiRequest("/api/v1/org/exports", { method: "POST" })
                .then(() => {
                  setNotice("Export ZIP generated.");
                  return load();
                })
                .catch((err) => setError(err instanceof Error ? err.message : "Export failed"))
            }
          >
            Generate Org Data Export
          </button>
          {exports.map((ex) => (
            <div key={ex.id} className="row" style={{ justifyContent: "space-between", fontSize: 12 }}>
              <span className="mono">{ex.id}</span>
              <span>{ex.status}</span>
            </div>
          ))}
          <hr style={{ borderColor: "var(--border)", margin: "12px 0" }} />
          <div className="row">
            <input
              className="input"
              style={{ flex: 1 }}
              placeholder={`Type org slug '${orgSlug}' to confirm`}
              value={hardDeleteSlug}
              onChange={(e) => setHardDeleteSlug(e.target.value)}
            />
            <button
              type="button"
              className="btn btn-sm btn-danger"
              onClick={() =>
                void apiRequest("/api/v1/org/hard-delete", {
                  method: "POST",
                  body: JSON.stringify({ confirm_slug: hardDeleteSlug }),
                })
                  .then(() => setNotice("Organization hard deleted."))
                  .catch((err) => setError(err instanceof Error ? err.message : "Hard delete denied"))
              }
            >
              Hard Delete Org
            </button>
          </div>
        </div>
      </div>

      <div className="card">
        <h3 className="card-title">
          <span>
            <Shield size={16} /> Audit Trail & Support Grants
          </span>
        </h3>
        <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 8 }}>
          Active/Historical Support Grants: {grants.length} • Metrics endpoint: <code>/api/v1/metrics</code>
        </div>
        {isPlatformOperator && (
          <div style={{ marginBottom: 12 }}>
            <strong>Operator Kill Switches ({switches.length})</strong>
          </div>
        )}
        <table className="table">
          <thead>
            <tr>
              <th>Action</th>
              <th>Target</th>
              <th>Timestamp</th>
            </tr>
          </thead>
          <tbody>
            {audits.slice(0, 15).map((a) => (
              <tr key={a.id}>
                <td className="mono">{a.action}</td>
                <td>{a.target_type}</td>
                <td style={{ fontSize: 12, color: "var(--text-muted)" }}>{a.created_at}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
};
