import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  CheckCircle2,
  Eye,
  FileCode2,
  GitPullRequest,
  Play,
  RefreshCw,
  Rocket,
  ShieldAlert,
  Sparkles,
  Trash2,
  XCircle,
} from "lucide-react";
import { apiRequest, getStoredToken, randomKey } from "../api/client";

export interface ProjectItem {
  id: string;
  name: string;
  slug: string;
}

export interface RepoItem {
  id: string;
  project_id: string;
  full_name: string;
  support_profile: string;
  status: string;
}

interface TaskSummary {
  id: string;
  project_id: string;
  title: string;
  requirement_text: string;
  task_type: string;
  risk_level: string;
  status: string;
  current_stage: string;
  cleanup_status: string;
  repo_ids: string[];
  budget_cap_cents: number;
  spent_cents: number;
  active_run_id: string | null;
  error_code: string | null;
  error_message: string | null;
  created_at: string;
}

interface ArtifactSummary {
  id: string;
  artifact_type: string;
  version: number;
  schema_version: string;
  sha256: string;
  metadata: Record<string, unknown>;
  created_at: string;
}

interface ApprovalSummary {
  id: string;
  gate_type: string;
  decision: string;
  target_digest: string;
  actor_user_id: string;
  is_valid: boolean;
  invalidated_reason: string | null;
  created_at: string;
}

interface TaskDetail extends TaskSummary {
  run: {
    id: string;
    status: string;
    current_node: string;
    base_commits: Record<string, string>;
    policy_version: string;
    spec_version: number;
    spec_sha256: string | null;
    plan_sha256: string | null;
    patch_sha256: string | null;
    verification_verdict: string | null;
  } | null;
  artifacts: ArtifactSummary[];
  approvals: ApprovalSummary[];
}

interface TaskEventItem {
  seq: number;
  event_id: string;
  event_type: string;
  stage: string;
  payload: Record<string, unknown>;
  created_at?: string;
}

interface PrItem {
  id: string;
  branch_name: string;
  base_branch: string;
  external_pr_number: number | null;
  external_pr_url: string | null;
  status: string;
  ci_status: string;
  release_notes: string;
}

interface FindingItem {
  id: string;
  severity: string;
  file_path: string;
  line_number: number;
  title: string;
  detail: string;
  status: string;
}

interface ScrItem {
  id: string;
  raised_by_role: string;
  reason: string;
  status: string;
  created_at: string;
}

interface VerificationReportItem {
  id: string;
  patch_sha256: string;
  verdict: string;
  created_at: string;
  traceability: Array<{
    id: string;
    ac_id: string;
    status: string;
    linked_test_cases: string[];
    observed_test_ids: string[];
    manual_confirmed_by: string | null;
  }>;
}

const PIPELINE_STAGES = [
  { key: "intake_and_budget", label: "1. Intake" },
  { key: "snapshot_context", label: "2. Context" },
  { key: "ba_specify", label: "3. BA Spec" },
  { key: "clarify_gate", label: "4. Clarify" },
  { key: "architect_design", label: "5. Architect" },
  { key: "qa_critique_testplan", label: "6. QA Plan" },
  { key: "spec_approval_gate", label: "7. Spec Gate" },
  { key: "execution_plan", label: "8. Exec Plan" },
  { key: "plan_approval_gate", label: "9. Plan Gate" },
  { key: "code_and_tdd", label: "10. Coding" },
  { key: "independent_verify", label: "11. Verify" },
  { key: "code_review", label: "12. Review" },
  { key: "delivery_approval_gate", label: "13. Delivery Gate" },
  { key: "publish_pr", label: "14. PR Publish" },
  { key: "preview_and_release_gate", label: "15. Release" },
  { key: "cleanup_and_settle", label: "16. Settle" },
] as const;

function statusPillClass(status: string): string {
  if (status === "DELIVERED" || status === "PASS" || status === "passed") return "pill pill-success";
  if (status === "FAILED" || status === "DEAD_LETTER" || status === "failed" || status === "FAIL") return "pill pill-danger";
  if (status.startsWith("WAITING")) return "pill pill-warn";
  return "pill pill-info";
}

function detectPendingGate(detail: TaskDetail | null): "spec" | "plan" | "delivery" | "release" | null {
  if (!detail || !detail.run) return null;
  const node = detail.run.current_node || detail.current_stage;
  if (node === "spec_approval_gate") return "spec";
  if (node === "plan_approval_gate") return "plan";
  if (node === "delivery_approval_gate") return "delivery";
  if (node === "preview_and_release_gate") return "release";
  if (detail.status === "WAITING_APPROVAL") {
    if (detail.run.patch_sha256) return "delivery";
    if (detail.run.plan_sha256) return "plan";
    if (detail.run.spec_sha256) return "spec";
  }
  return null;
}

interface TaskWorkbenchProps {
  projects: ProjectItem[];
  repos: RepoItem[];
  onRefreshCore: () => Promise<void>;
}

export const TaskWorkbench: React.FC<TaskWorkbenchProps> = ({ projects, repos, onRefreshCore }) => {
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [selectedTaskId, setSelectedTaskId] = useState<string>("");
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const [events, setEvents] = useState<TaskEventItem[]>([]);
  const [sseState, setSseState] = useState<"idle" | "connected" | "closed">("idle");
  const [prs, setPrs] = useState<PrItem[]>([]);
  const [findings, setFindings] = useState<FindingItem[]>([]);
  const [scrs, setScrs] = useState<ScrItem[]>([]);
  const [verificationReports, setVerificationReports] = useState<VerificationReportItem[]>([]);
  const [testPlan, setTestPlan] = useState<Record<string, unknown> | null>(null);
  const [artifactView, setArtifactView] = useState<{ title: string; body: string } | null>(null);
  const [previewInfo, setPreviewInfo] = useState<{ preview_id: string; view?: Record<string, unknown> } | null>(null);
  const [releaseNotes, setReleaseNotes] = useState<Record<string, unknown> | null>(null);

  // Composer state
  const [projectId, setProjectId] = useState<string>("");
  const [title, setTitle] = useState("");
  const [reqText, setReqText] = useState("");
  const [taskType, setTaskType] = useState("feature");
  const [riskLevel, setRiskLevel] = useState("standard");
  const [fastTrack, setFastTrack] = useState(false);
  const [requireRelease, setRequireRelease] = useState(false);
  const [selectedRepos, setSelectedRepos] = useState<string[]>([]);

  // Interaction state
  const [clarifyAnswer, setClarifyAnswer] = useState("");
  const [specSummaryEdit, setSpecSummaryEdit] = useState("");
  const [gateComment, setGateComment] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const sseRef = useRef<EventSource | null>(null);

  useEffect(() => {
    if (!projectId && projects.length > 0) {
      setProjectId(projects[0].id);
    }
  }, [projects, projectId]);

  const projectRepos = useMemo(
    () => repos.filter((r) => !projectId || r.project_id === projectId),
    [repos, projectId],
  );

  const loadTasks = useCallback(async () => {
    try {
      const list = await apiRequest<TaskSummary[]>("/api/v1/tasks");
      setTasks(list);
      if (list.length > 0 && !selectedTaskId) {
        setSelectedTaskId(list[0].id);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load tasks");
    }
  }, [selectedTaskId]);

  const loadTaskDetails = useCallback(async (taskId: string) => {
    if (!taskId) return;
    try {
      const [d, evLog, prRows, findingRows, scrRows, verData] = await Promise.all([
        apiRequest<TaskDetail>(`/api/v1/tasks/${taskId}`),
        apiRequest<TaskEventItem[]>(`/api/v1/tasks/${taskId}/events/log`),
        apiRequest<PrItem[]>(`/api/v1/tasks/${taskId}/prs`).catch(() => []),
        apiRequest<FindingItem[]>(`/api/v1/tasks/${taskId}/findings`).catch(() => []),
        apiRequest<ScrItem[]>(`/api/v1/tasks/${taskId}/scrs`).catch(() => []),
        apiRequest<{ reports: VerificationReportItem[] }>(`/api/v1/tasks/${taskId}/verification`).catch(() => ({
          reports: [],
        })),
      ]);
      setDetail(d);
      setEvents(evLog);
      setPrs(prRows);
      setFindings(findingRows);
      setScrs(scrRows);
      setVerificationReports(verData.reports || []);
      const tp = await apiRequest<Record<string, unknown>>(`/api/v1/tasks/${taskId}/test-plan`).catch(() => null);
      setTestPlan(tp);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to inspect task");
    }
  }, []);

  useEffect(() => {
    void loadTasks();
  }, [loadTasks]);

  useEffect(() => {
    if (!selectedTaskId) return;
    void loadTaskDetails(selectedTaskId);
    setArtifactView(null);
    setPreviewInfo(null);
    setReleaseNotes(null);
  }, [selectedTaskId, loadTaskDetails]);

  // Live SSE subscription for active task
  useEffect(() => {
    if (sseRef.current) {
      sseRef.current.close();
      sseRef.current = null;
    }
    if (!selectedTaskId) {
      setSseState("idle");
      return;
    }
    const token = getStoredToken();
    if (!token) return;
    const url = `/api/v1/tasks/${encodeURIComponent(selectedTaskId)}/events?follow=true&token=${encodeURIComponent(token)}`;
    const es = new EventSource(url);
    sseRef.current = es;
    es.onopen = () => setSseState("connected");

    const handleFrame = (evt: MessageEvent) => {
      try {
        const parsed = JSON.parse(evt.data) as TaskEventItem;
        setEvents((prev) => {
          if (prev.some((e) => e.seq === parsed.seq)) return prev;
          return [...prev, parsed].sort((a, b) => a.seq - b.seq);
        });
      } catch {
        // Ignore heartbeat or malformed frame
      }
    };

    const eventNames = [
      "message",
      "task.created",
      "stage.started",
      "stage.completed",
      "gate.waiting",
      "gate.approved",
      "gate.rejected",
      "task.finished",
      "task.cancelled",
      "task.clarification_required",
      "task.clarification_answered",
      "pr.published",
    ];
    for (const name of eventNames) {
      es.addEventListener(name, handleFrame as EventListener);
    }
    es.onerror = () => {
      setSseState("closed");
      es.close();
    };
    return () => {
      es.close();
    };
  }, [selectedTaskId, detail?.status]);

  async function refreshAll(taskId?: string) {
    await loadTasks();
    const target = taskId || selectedTaskId;
    if (target) await loadTaskDetails(target);
  }

  async function handleCreateTask(e: React.FormEvent) {
    e.preventDefault();
    if (!projectId) {
      setError("Create a project in Projects & Repos first.");
      return;
    }
    setError("");
    setNotice("");
    setBusy(true);
    try {
      const created = await apiRequest<TaskSummary>("/api/v1/tasks", {
        method: "POST",
        idempotencyKey: randomKey("task"),
        body: JSON.stringify({
          project_id: projectId,
          title,
          requirement_text: reqText,
          task_type: taskType,
          repo_ids: selectedRepos,
          risk_level: riskLevel,
          fast_track: fastTrack,
          require_release_approval: requireRelease,
        }),
      });
      setTitle("");
      setReqText("");
      setSelectedTaskId(created.id);
      setNotice(`Task ${created.id} queued.`);
      await refreshAll(created.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create task");
    } finally {
      setBusy(false);
    }
  }

  async function handleDrainWorker() {
    setError("");
    setBusy(true);
    try {
      const res = await apiRequest<{ processed_count: number }>("/api/v1/workers/drain?max_jobs=10", {
        method: "POST",
      });
      setNotice(`Worker processed ${res.processed_count} job(s).`);
      await refreshAll();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Worker drain failed");
    } finally {
      setBusy(false);
    }
  }

  async function handleGateDecision(decision: "approved" | "rejected") {
    if (!detail || !detail.run) return;
    const gate = detectPendingGate(detail);
    if (!gate) return;
    const digest =
      gate === "spec"
        ? detail.run.spec_sha256 || ""
        : gate === "plan"
          ? detail.run.plan_sha256 || ""
          : detail.run.patch_sha256 || detail.run.plan_sha256 || detail.run.spec_sha256 || "docs-patch";
    const baseSha = Object.values(detail.run.base_commits || {})[0] || "docs-base";

    setError("");
    setBusy(true);
    try {
      await apiRequest(`/api/v1/tasks/${detail.id}/approvals`, {
        method: "POST",
        idempotencyKey: randomKey(`apr-${gate}`),
        body: JSON.stringify({
          gate_type: gate,
          decision,
          target_digest: digest,
          base_commit_sha: baseSha,
          policy_version: detail.run.policy_version || "policy-v1",
          comment: gateComment,
        }),
      });
      setGateComment("");
      setNotice(`Gate '${gate}' marked as ${decision}. Run Worker Drain to advance.`);
      await refreshAll();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Approval failed");
    } finally {
      setBusy(false);
    }
  }

  async function handleClarifySubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!detail) return;
    setError("");
    setBusy(true);
    try {
      await apiRequest(`/api/v1/tasks/${detail.id}/clarify`, {
        method: "POST",
        idempotencyKey: randomKey("clarify"),
        body: JSON.stringify({ answers: { response: clarifyAnswer } }),
      });
      setClarifyAnswer("");
      setNotice("Clarification submitted. Drain worker to resume.");
      await refreshAll();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Clarification failed");
    } finally {
      setBusy(false);
    }
  }

  async function handleEditSpec(e: React.FormEvent) {
    e.preventDefault();
    if (!detail || !specSummaryEdit.trim()) return;
    setError("");
    setBusy(true);
    try {
      await apiRequest(`/api/v1/tasks/${detail.id}/spec`, {
        method: "PUT",
        body: JSON.stringify({ summary: specSummaryEdit }),
      });
      setSpecSummaryEdit("");
      setNotice("Specification updated cleanly (previous approvals invalidated if digest changed).");
      await refreshAll();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Spec update failed");
    } finally {
      setBusy(false);
    }
  }

  async function handleViewArtifact(art: ArtifactSummary) {
    if (!detail) return;
    try {
      const data = await apiRequest<{ content: unknown }>(`/api/v1/tasks/${detail.id}/artifacts/${art.id}/content`);
      setArtifactView({
        title: `${art.artifact_type} v${art.version} (${art.sha256.slice(0, 12)})`,
        body: typeof data.content === "string" ? data.content : JSON.stringify(data.content, null, 2),
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load artifact");
    }
  }

  async function handleLaunchPreview() {
    if (!detail) return;
    setError("");
    setNotice("");
    setBusy(true);
    try {
      const created = await apiRequest<{ preview_id: string }>(`/api/v1/tasks/${detail.id}/previews`, {
        method: "POST",
        body: JSON.stringify({ ttl_seconds: 1800 }),
      });
      const view = await apiRequest<Record<string, unknown>>(`/api/v1/previews/${created.preview_id}/view`);
      setPreviewInfo({ preview_id: created.preview_id, view });
      setNotice(`Private preview ${created.preview_id} launched.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Preview launch failed");
    } finally {
      setBusy(false);
    }
  }

  async function handleDestroyPreview(previewId: string) {
    setError("");
    try {
      await apiRequest(`/api/v1/previews/${previewId}`, { method: "DELETE" });
      setPreviewInfo(null);
      setNotice("Preview torn down.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to destroy preview");
    }
  }

  async function handlePromoteRelease() {
    if (!detail) return;
    setError("");
    setBusy(true);
    try {
      const notes = await apiRequest<Record<string, unknown>>(`/api/v1/tasks/${detail.id}/release-notes`);
      setReleaseNotes(notes);
      const promoted = await apiRequest<{ status?: string }>(`/api/v1/tasks/${detail.id}/release`, {
        method: "POST",
        idempotencyKey: randomKey("rel"),
        body: JSON.stringify({ comment: "Promoted from Web UI" }),
      });
      setNotice(`Release status: ${promoted.status || "promoted"}`);
      await refreshAll();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Release promotion denied");
    } finally {
      setBusy(false);
    }
  }

  async function handleCancelTask() {
    if (!detail) return;
    setError("");
    try {
      await apiRequest(`/api/v1/tasks/${detail.id}/cancel`, { method: "POST" });
      setNotice("Task cancelled.");
      await refreshAll();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Cancel failed");
    }
  }

  const pendingGate = detectPendingGate(detail);
  const currentStageKey = detail?.run?.current_node || detail?.current_stage || "";
  const activeStageIdx = PIPELINE_STAGES.findIndex((s) => s.key === currentStageKey);

  return (
    <div>
      {error && (
        <div className="alert alert-error" role="alert" data-testid="workbench-error">
          {error}
        </div>
      )}
      {notice && (
        <div className="alert alert-ok" data-testid="workbench-notice">
          {notice}
        </div>
      )}

      <div className="grid-2">
        {/* Left Sidebar: Task Composer & Queue */}
        <div>
          <div className="card">
            <h2 className="card-title">
              <span>
                <Sparkles size={16} /> New SDLC Task
              </span>
              <button
                type="button"
                className="btn btn-sm"
                onClick={() => void onRefreshCore()}
                title="Refresh projects & repos"
              >
                <RefreshCw size={13} />
              </button>
            </h2>
            <form onSubmit={handleCreateTask}>
              <div className="field">
                <label>Project</label>
                <select
                  className="select"
                  value={projectId}
                  onChange={(e) => setProjectId(e.target.value)}
                  data-testid="select-task-project"
                >
                  {projects.length === 0 && <option value="">No project created yet</option>}
                  {projects.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label>Title</label>
                <input
                  className="input"
                  required
                  minLength={2}
                  value={title}
                  onChange={(e) => setTitle(e.target.value)}
                  placeholder="Implement health check and subtraction helper"
                  data-testid="input-task-title"
                />
              </div>
              <div className="field">
                <label>Requirement Specification</label>
                <textarea
                  className="textarea"
                  rows={3}
                  required
                  minLength={5}
                  value={reqText}
                  onChange={(e) => setReqText(e.target.value)}
                  placeholder="Describe expected behavior, files, and acceptance criteria..."
                  data-testid="input-task-req"
                />
              </div>
              <div className="row" style={{ marginBottom: 12 }}>
                <div className="field" style={{ flex: 1, marginBottom: 0 }}>
                  <label>Type</label>
                  <select className="select" value={taskType} onChange={(e) => setTaskType(e.target.value)}>
                    <option value="feature">feature</option>
                    <option value="bugfix">bugfix</option>
                    <option value="docs">docs</option>
                    <option value="refactor">refactor</option>
                  </select>
                </div>
                <div className="field" style={{ flex: 1, marginBottom: 0 }}>
                  <label>Risk</label>
                  <select className="select" value={riskLevel} onChange={(e) => setRiskLevel(e.target.value)}>
                    <option value="standard">standard</option>
                    <option value="high">high</option>
                  </select>
                </div>
              </div>

              {projectRepos.length > 0 && (
                <div className="field">
                  <label>Target Repositories</label>
                  {projectRepos.map((r) => (
                    <label key={r.id} className="row" style={{ fontWeight: 400, color: "var(--text)" }}>
                      <input
                        type="checkbox"
                        checked={selectedRepos.includes(r.id)}
                        onChange={(e) =>
                          setSelectedRepos((prev) =>
                            e.target.checked ? [...prev, r.id] : prev.filter((id) => id !== r.id),
                          )
                        }
                        data-testid={`chk-repo-${r.id}`}
                      />
                      <span>
                        {r.full_name} ({r.support_profile})
                      </span>
                    </label>
                  ))}
                </div>
              )}

              <div className="row" style={{ marginBottom: 12 }}>
                <label className="row" style={{ fontSize: 12 }}>
                  <input type="checkbox" checked={fastTrack} onChange={(e) => setFastTrack(e.target.checked)} />
                  <span>Fast Track</span>
                </label>
                <label className="row" style={{ fontSize: 12 }}>
                  <input
                    type="checkbox"
                    checked={requireRelease}
                    onChange={(e) => setRequireRelease(e.target.checked)}
                  />
                  <span>Release Gate</span>
                </label>
              </div>

              <button
                type="submit"
                className="btn btn-primary"
                style={{ width: "100%" }}
                disabled={busy}
                data-testid="btn-create-task"
              >
                Queue Task
              </button>
            </form>
          </div>

          <div className="card">
            <h2 className="card-title">
              <span>Task Queue ({tasks.length})</span>
              <div className="row">
                <button
                  type="button"
                  className="btn btn-sm btn-primary"
                  onClick={() => void handleDrainWorker()}
                  disabled={busy}
                  data-testid="btn-drain-worker"
                >
                  <Play size={12} /> Drain Worker
                </button>
                <button type="button" className="btn btn-sm" onClick={() => void refreshAll()}>
                  <RefreshCw size={12} />
                </button>
              </div>
            </h2>
            {tasks.length === 0 && <div style={{ color: "var(--text-muted)" }}>No tasks queued yet.</div>}
            {tasks.map((t) => (
              <div
                key={t.id}
                className={`task-item ${selectedTaskId === t.id ? "selected" : ""}`}
                onClick={() => setSelectedTaskId(t.id)}
                data-testid={`task-item-${t.id}`}
              >
                <div className="row" style={{ justifyContent: "space-between", marginBottom: 4 }}>
                  <strong style={{ fontSize: 13 }}>{t.title}</strong>
                  <span className={statusPillClass(t.status)}>{t.status}</span>
                </div>
                <div className="row" style={{ justifyContent: "space-between", fontSize: 11, color: "var(--text-muted)" }}>
                  <span className="mono">{t.current_stage}</span>
                  <span>
                    {t.spent_cents}/{t.budget_cap_cents}¢
                  </span>
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Right Workspace: Selected Task Deep Inspector */}
        <div>
          {!detail ? (
            <div className="card">Select or queue a task to inspect live pipeline progress and gates.</div>
          ) : (
            <>
              <div className="card">
                <div className="row" style={{ justifyContent: "space-between", marginBottom: 10 }}>
                  <div>
                    <h2 style={{ margin: 0, fontSize: 17 }}>{detail.title}</h2>
                    <div className="mono" style={{ fontSize: 12, color: "var(--text-muted)" }}>
                      {detail.id} • stage: {currentStageKey} • verification: {detail.run?.verification_verdict || "pending"}
                    </div>
                  </div>
                  <div className="row">
                    <span className={statusPillClass(detail.status)} data-testid="task-detail-status">
                      {detail.status}
                    </span>
                    <button
                      type="button"
                      className="btn btn-sm"
                      onClick={() => void handleDrainWorker()}
                      disabled={busy}
                    >
                      <Play size={12} /> Run Step
                    </button>
                    <button type="button" className="btn btn-sm btn-danger" onClick={() => void handleCancelTask()}>
                      <XCircle size={12} /> Cancel
                    </button>
                  </div>
                </div>

                {/* 16-stage visual stepper */}
                <div className="stage-strip">
                  {PIPELINE_STAGES.map((st, idx) => {
                    const cls =
                      idx === activeStageIdx
                        ? "stage-chip current"
                        : activeStageIdx > idx || detail.status === "DELIVERED"
                          ? "stage-chip done"
                          : "stage-chip";
                    return (
                      <span key={st.key} className={cls}>
                        {st.label}
                      </span>
                    );
                  })}
                </div>

                <div style={{ color: "var(--text-muted)", fontSize: 13 }}>{detail.requirement_text}</div>
                {detail.error_message && (
                  <div className="alert alert-error" style={{ marginTop: 10 }}>
                    {detail.error_code}: {detail.error_message}
                  </div>
                )}
              </div>

              {/* Clarification input */}
              {detail.status === "WAITING_INPUT" && (
                <div className="card" data-testid="clarify-panel">
                  <h3 className="card-title">Clarification Required</h3>
                  <form onSubmit={handleClarifySubmit}>
                    <div className="field">
                      <label>Provide Clarification Answers</label>
                      <textarea
                        className="textarea"
                        rows={3}
                        required
                        value={clarifyAnswer}
                        onChange={(e) => setClarifyAnswer(e.target.value)}
                        placeholder="Clarify expected acceptance criteria, target files, and constraints..."
                        data-testid="input-clarify-answer"
                      />
                    </div>
                    <button type="submit" className="btn btn-primary" disabled={busy} data-testid="btn-submit-clarify">
                      Submit Clarification
                    </button>
                  </form>
                </div>
              )}

              {/* Gate Approval Panel */}
              {pendingGate && (
                <div className="card" data-testid="gate-panel">
                  <h3 className="card-title">
                    <span>
                      <ShieldAlert size={16} color="#f59e0b" /> Approval Required: {pendingGate.toUpperCase()} Gate
                    </span>
                    <span className="pill pill-warn">{detail.run?.current_node}</span>
                  </h3>
                  <div className="row" style={{ marginBottom: 10 }}>
                    <input
                      className="input"
                      style={{ flex: 1 }}
                      placeholder="Optional review comment..."
                      value={gateComment}
                      onChange={(e) => setGateComment(e.target.value)}
                    />
                    <button
                      type="button"
                      className="btn btn-success"
                      disabled={busy}
                      onClick={() => void handleGateDecision("approved")}
                      data-testid="btn-approve-gate"
                    >
                      <CheckCircle2 size={15} /> Approve {pendingGate}
                    </button>
                    <button
                      type="button"
                      className="btn btn-danger"
                      disabled={busy}
                      onClick={() => void handleGateDecision("rejected")}
                      data-testid="btn-reject-gate"
                    >
                      <XCircle size={15} /> Reject
                    </button>
                  </div>
                </div>
              )}

              {/* Delivery: PRs, Previews, Release Promotion */}
              <div className="card">
                <h3 className="card-title">
                  <span>
                    <GitPullRequest size={16} /> Delivery, Private Previews & Release
                  </span>
                  <div className="row">
                    <button
                      type="button"
                      className="btn btn-sm"
                      disabled={busy}
                      onClick={() => void handleLaunchPreview()}
                      data-testid="btn-launch-preview"
                    >
                      <Eye size={13} /> Launch Preview
                    </button>
                    <button
                      type="button"
                      className="btn btn-sm btn-primary"
                      disabled={busy}
                      onClick={() => void handlePromoteRelease()}
                      data-testid="btn-promote-release"
                    >
                      <Rocket size={13} /> Promote Release
                    </button>
                  </div>
                </h3>

                {prs.length > 0 && (
                  <table className="table" style={{ marginBottom: 10 }}>
                    <thead>
                      <tr>
                        <th>Branch</th>
                        <th>PR #</th>
                        <th>Status</th>
                        <th>CI</th>
                        <th>Actions</th>
                      </tr>
                    </thead>
                    <tbody>
                      {prs.map((pr) => (
                        <tr key={pr.id}>
                          <td className="mono">{pr.branch_name}</td>
                          <td>{pr.external_pr_number ? `#${pr.external_pr_number}` : "local"}</td>
                          <td>
                            <span className={statusPillClass(pr.status)}>{pr.status}</span>
                          </td>
                          <td>
                            <span className={statusPillClass(pr.ci_status)}>{pr.ci_status}</span>
                          </td>
                          <td className="row">
                            <button
                              type="button"
                              className="btn btn-sm"
                              onClick={() =>
                                void apiRequest(`/api/v1/prs/${pr.id}/ci-feedback`, {
                                  method: "POST",
                                  body: JSON.stringify({ ci_status: "passed", summary: "Manual UI CI pass" }),
                                }).then(() => refreshAll())
                              }
                            >
                              CI Pass
                            </button>
                            <button
                              type="button"
                              className="btn btn-sm btn-danger"
                              onClick={() =>
                                void apiRequest(`/api/v1/prs/${pr.id}/ci-feedback`, {
                                  method: "POST",
                                  body: JSON.stringify({ ci_status: "failed", summary: "CI failure simulation" }),
                                }).then(() => refreshAll())
                              }
                            >
                              CI Fail
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}

                {previewInfo && (
                  <div className="alert alert-ok" data-testid="preview-panel">
                    <div className="row" style={{ justifyContent: "space-between" }}>
                      <span className="mono">Preview {previewInfo.preview_id}</span>
                      <button
                        type="button"
                        className="btn btn-sm btn-danger"
                        onClick={() => void handleDestroyPreview(previewInfo.preview_id)}
                      >
                        <Trash2 size={12} /> Destroy Preview
                      </button>
                    </div>
                    <pre className="code-box" style={{ marginTop: 8 }}>
                      {JSON.stringify(previewInfo.view, null, 2)}
                    </pre>
                  </div>
                )}

                {releaseNotes && (
                  <pre className="code-box" style={{ marginTop: 8 }}>
                    {JSON.stringify(releaseNotes, null, 2)}
                  </pre>
                )}
              </div>

              {/* Verification, Traceability & SCR / Findings */}
              <div className="grid-cards">
                <div className="card">
                  <h3 className="card-title">Verification & Traceability</h3>
                  {verificationReports.length === 0 && (
                    <div style={{ color: "var(--text-muted)" }}>No verification report recorded yet.</div>
                  )}
                  {verificationReports.map((rep) => (
                    <div key={rep.id} style={{ marginBottom: 10 }}>
                      <div className="row" style={{ justifyContent: "space-between", marginBottom: 6 }}>
                        <span className="mono">{rep.id}</span>
                        <span className={statusPillClass(rep.verdict)}>{rep.verdict}</span>
                      </div>
                      {rep.traceability.map((tr) => (
                        <div key={tr.id} className="row" style={{ justifyContent: "space-between", padding: "3px 0" }}>
                          <span>
                            {tr.ac_id}: <strong>{tr.status}</strong>
                          </span>
                          {tr.status === "manual_required" && (
                            <button
                              type="button"
                              className="btn btn-sm"
                              onClick={() =>
                                void apiRequest(
                                  `/api/v1/verification-reports/${rep.id}/traceability/${tr.ac_id}/confirm`,
                                  { method: "POST" },
                                )
                                  .then(() => refreshAll())
                                  .catch((e) => setError(e instanceof Error ? e.message : "Failed"))
                              }
                            >
                              Confirm Manual
                            </button>
                          )}
                        </div>
                      ))}
                    </div>
                  ))}
                </div>

                <div className="card">
                  <h3 className="card-title">Spec Edit, SCRs & Findings</h3>
                  <form onSubmit={handleEditSpec} style={{ marginBottom: 10 }}>
                    <div className="row">
                      <input
                        className="input"
                        style={{ flex: 1 }}
                        placeholder="Edit spec summary (invalidates stale approval)..."
                        value={specSummaryEdit}
                        onChange={(e) => setSpecSummaryEdit(e.target.value)}
                      />
                      <button type="submit" className="btn btn-sm" disabled={busy}>
                        Save Spec
                      </button>
                    </div>
                  </form>
                  {scrs.map((s) => (
                    <div key={s.id} className="row" style={{ justifyContent: "space-between", marginBottom: 6 }}>
                      <span>
                        SCR ({s.raised_by_role}): {s.reason} [{s.status}]
                      </span>
                      {s.status !== "resolved" && (
                        <button
                          type="button"
                          className="btn btn-sm"
                          onClick={() =>
                            void apiRequest(`/api/v1/tasks/${detail.id}/scrs/${s.id}/resolve`, {
                              method: "POST",
                              body: JSON.stringify({ resolution_note: "Resolved in workbench" }),
                            }).then(() => refreshAll())
                          }
                        >
                          Resolve
                        </button>
                      )}
                    </div>
                  ))}
                  {findings.map((f) => (
                    <div key={f.id} style={{ fontSize: 12, padding: "4px 0", borderTop: "1px solid var(--border)" }}>
                      <span className="pill pill-warn">{f.severity}</span> <strong>{f.title}</strong> ({f.file_path}:
                      {f.line_number})
                    </div>
                  ))}
                  {testPlan && (
                    <details style={{ marginTop: 8 }}>
                      <summary style={{ cursor: "pointer" }}>QA Test Plan ({String(testPlan.verdict || "")})</summary>
                      <pre className="code-box">{JSON.stringify(testPlan, null, 2)}</pre>
                    </details>
                  )}
                </div>
              </div>

              {/* Artifacts & Approval Audit */}
              <div className="card">
                <h3 className="card-title">
                  <span>
                    <FileCode2 size={16} /> Artifacts ({detail.artifacts.length}) & Approvals ({detail.approvals.length})
                  </span>
                </h3>
                <div className="row" style={{ marginBottom: 10 }}>
                  {detail.artifacts.map((a) => (
                    <button
                      key={a.id}
                      type="button"
                      className="btn btn-sm"
                      onClick={() => void handleViewArtifact(a)}
                    >
                      {a.artifact_type} v{a.version}
                    </button>
                  ))}
                </div>
                {artifactView && (
                  <div style={{ marginBottom: 10 }}>
                    <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 4 }}>{artifactView.title}</div>
                    <pre className="code-box">{artifactView.body}</pre>
                  </div>
                )}
                {detail.approvals.length > 0 && (
                  <table className="table">
                    <thead>
                      <tr>
                        <th>Gate</th>
                        <th>Decision</th>
                        <th>Digest</th>
                        <th>Valid</th>
                      </tr>
                    </thead>
                    <tbody>
                      {detail.approvals.map((ap) => (
                        <tr key={ap.id}>
                          <td>{ap.gate_type}</td>
                          <td>{ap.decision}</td>
                          <td className="mono">{ap.target_digest.slice(0, 14)}</td>
                          <td>{ap.is_valid ? "yes" : `no (${ap.invalidated_reason || ""})`}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>

              {/* Live SSE Timeline */}
              <div className="card" data-testid="sse-timeline">
                <h3 className="card-title">
                  <span>Live Event Stream</span>
                  <span className={`pill ${sseState === "connected" ? "pill-success" : "pill-info"}`}>
                    SSE: {sseState} ({events.length})
                  </span>
                </h3>
                <div className="code-box" style={{ maxHeight: 220 }}>
                  {events.length === 0
                    ? "Waiting for task events..."
                    : events
                        .map(
                          (ev) =>
                            `#${ev.seq} [${ev.stage}] ${ev.event_type} ${JSON.stringify(ev.payload)}`,
                        )
                        .join("\n")}
                </div>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
};
