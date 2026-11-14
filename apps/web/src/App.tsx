import React, { useCallback, useEffect, useState } from "react";
import { Cpu, CreditCard, FolderGit2, Layers, LogOut, Shield, TerminalSquare } from "lucide-react";
import { apiRequest, getStoredToken, setStoredToken } from "./api/client";
import type { MeResponse, TenantSummary } from "./api/generated";
import {
  BillingSection,
  GovernanceSection,
  ModelsSection,
  ProjectsSection,
} from "./components/AdminSections";
import { AuthView } from "./components/AuthView";
import { TaskWorkbench, type ProjectItem, type RepoItem } from "./components/TaskWorkbench";

type NavTab = "tasks" | "projects" | "models" | "billing" | "governance";

export const App: React.FC = () => {
  const [me, setMe] = useState<MeResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState<NavTab>("tasks");
  const [projects, setProjects] = useState<ProjectItem[]>([]);
  const [repos, setRepos] = useState<RepoItem[]>([]);
  const [error, setError] = useState("");

  const loadCoreData = useCallback(async () => {
    const [p, r] = await Promise.all([
      apiRequest<ProjectItem[]>("/api/v1/projects").catch(() => []),
      apiRequest<RepoItem[]>("/api/v1/repos").catch(() => []),
    ]);
    setProjects(p);
    setRepos(r);
  }, []);

  const bootstrapSession = useCallback(async () => {
    const token = getStoredToken();
    if (!token) {
      setMe(null);
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      const profile = await apiRequest<MeResponse>("/api/v1/auth/me");
      setMe(profile);
      await loadCoreData();
    } catch {
      setStoredToken("");
      setMe(null);
    } finally {
      setLoading(false);
    }
  }, [loadCoreData]);

  useEffect(() => {
    void bootstrapSession();
  }, [bootstrapSession]);

  async function handleSwitchOrg(tenantId: string) {
    setError("");
    try {
      await apiRequest("/api/v1/orgs/switch", {
        method: "POST",
        body: JSON.stringify({ tenant_id: tenantId }),
      });
      await bootstrapSession();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to switch organization");
    }
  }

  async function handleLogout() {
    try {
      await apiRequest("/api/v1/auth/logout", { method: "POST" });
    } catch {
      // Clear local session regardless
    }
    setStoredToken("");
    setMe(null);
  }

  if (loading) {
    return (
      <div className="auth-wrapper">
        <div className="card">Loading AI SDLC Control Plane...</div>
      </div>
    );
  }

  if (!me) {
    return <AuthView onAuthenticated={bootstrapSession} />;
  }

  const activeOrg: TenantSummary | null = me.active_org;
  const activeRole = activeOrg?.role || "member";

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">
          <TerminalSquare size={18} color="#6366f1" />
          <span>AI SDLC SaaS</span>
        </div>

        <nav className="nav-tabs">
          <button
            type="button"
            className={`nav-tab ${tab === "tasks" ? "active" : ""}`}
            onClick={() => setTab("tasks")}
            data-testid="nav-tasks"
          >
            <Layers size={14} /> Tasks
          </button>
          <button
            type="button"
            className={`nav-tab ${tab === "projects" ? "active" : ""}`}
            onClick={() => setTab("projects")}
            data-testid="nav-projects"
          >
            <FolderGit2 size={14} /> Projects & Repos
          </button>
          <button
            type="button"
            className={`nav-tab ${tab === "models" ? "active" : ""}`}
            onClick={() => setTab("models")}
            data-testid="nav-models"
          >
            <Cpu size={14} /> Models
          </button>
          <button
            type="button"
            className={`nav-tab ${tab === "billing" ? "active" : ""}`}
            onClick={() => setTab("billing")}
            data-testid="nav-billing"
          >
            <CreditCard size={14} /> Billing
          </button>
          <button
            type="button"
            className={`nav-tab ${tab === "governance" ? "active" : ""}`}
            onClick={() => setTab("governance")}
            data-testid="nav-governance"
          >
            <Shield size={14} /> Governance
          </button>
        </nav>

        <div className="topbar-right">
          {me.orgs.length > 0 && activeOrg && (
            <select
              className="select"
              style={{ width: "auto", padding: "4px 8px" }}
              value={activeOrg.id}
              onChange={(e) => void handleSwitchOrg(e.target.value)}
              data-testid="select-active-org"
            >
              {me.orgs.map((o) => (
                <option key={o.id} value={o.id}>
                  {o.name} ({o.role || "member"})
                </option>
              ))}
            </select>
          )}
          <span className="pill pill-info" data-testid="user-role-badge">
            {me.user.email} • {activeRole}
          </span>
          <button
            type="button"
            className="btn btn-sm"
            onClick={() => void handleLogout()}
            data-testid="btn-logout"
          >
            <LogOut size={13} /> Sign Out
          </button>
        </div>
      </header>

      <main className="main-container">
        {error && <div className="alert alert-error">{error}</div>}
        {tab === "tasks" && (
          <TaskWorkbench projects={projects} repos={repos} onRefreshCore={loadCoreData} />
        )}
        {tab === "projects" && (
          <ProjectsSection projects={projects} repos={repos} onRefreshCore={loadCoreData} />
        )}
        {tab === "models" && <ModelsSection />}
        {tab === "billing" && <BillingSection />}
        {tab === "governance" && (
          <GovernanceSection
            orgSlug={activeOrg?.slug || ""}
            isPlatformOperator={me.user.is_platform_operator}
          />
        )}
      </main>
    </div>
  );
};
