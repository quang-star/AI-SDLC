import React, { useState } from "react";
import { ShieldCheck } from "lucide-react";
import { apiRequest, setStoredToken } from "../api/client";

interface AuthViewProps {
  onAuthenticated: () => Promise<void>;
}

export const AuthView: React.FC<AuthViewProps> = ({ onAuthenticated }) => {
  const [mode, setMode] = useState<"login" | "register" | "reset">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [fullName, setFullName] = useState("");
  const [orgName, setOrgName] = useState("");
  const [resetToken, setResetToken] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setNotice("");
    setBusy(true);
    try {
      if (mode === "login") {
        const res = await apiRequest<{ access_token: string }>("/api/v1/auth/login", {
          method: "POST",
          body: JSON.stringify({ email, password }),
        });
        setStoredToken(res.access_token);
        await onAuthenticated();
      } else if (mode === "register") {
        const res = await apiRequest<{ access_token: string }>("/api/v1/auth/register", {
          method: "POST",
          body: JSON.stringify({
            email,
            password,
            full_name: fullName || "Team Lead",
            org_name: orgName || "Workspace",
          }),
        });
        setStoredToken(res.access_token);
        await onAuthenticated();
      } else if (!resetToken) {
        const res = await apiRequest<{ reset_token?: string; message?: string }>(
          "/api/v1/auth/password-reset/request",
          { method: "POST", body: JSON.stringify({ email }) },
        );
        if (res.reset_token) {
          setResetToken(res.reset_token);
          setNotice("Password reset token generated for local dev mode.");
        } else {
          setNotice("If an account exists for that email, a reset link was dispatched.");
        }
      } else {
        await apiRequest("/api/v1/auth/password-reset/confirm", {
          method: "POST",
          body: JSON.stringify({ token: resetToken, new_password: newPassword }),
        });
        setNotice("Password updated. You may sign in now.");
        setMode("login");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Authentication failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="auth-wrapper">
      <div className="card auth-card">
        <div className="row" style={{ marginBottom: 14 }}>
          <ShieldCheck size={22} color="#6366f1" />
          <h1 style={{ margin: 0, fontSize: 18 }}>AI SDLC Control Plane</h1>
        </div>
        <div className="row" style={{ marginBottom: 16 }}>
          <button
            type="button"
            className={`btn btn-sm ${mode === "login" ? "btn-primary" : ""}`}
            onClick={() => setMode("login")}
            data-testid="tab-login"
          >
            Sign In
          </button>
          <button
            type="button"
            className={`btn btn-sm ${mode === "register" ? "btn-primary" : ""}`}
            onClick={() => setMode("register")}
            data-testid="tab-register"
          >
            Create Org
          </button>
          <button
            type="button"
            className={`btn btn-sm ${mode === "reset" ? "btn-primary" : ""}`}
            onClick={() => setMode("reset")}
            data-testid="tab-reset"
          >
            Reset Password
          </button>
        </div>

        {error && <div className="alert alert-error" role="alert">{error}</div>}
        {notice && <div className="alert alert-ok">{notice}</div>}

        <form onSubmit={handleSubmit}>
          <div className="field">
            <label htmlFor="auth-email">Work Email</label>
            <input
              id="auth-email"
              data-testid="input-email"
              className="input"
              type="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="lead@acme.dev"
            />
          </div>

          {mode === "register" && (
            <>
              <div className="field">
                <label htmlFor="auth-name">Full Name</label>
                <input
                  id="auth-name"
                  data-testid="input-fullname"
                  className="input"
                  required
                  value={fullName}
                  onChange={(e) => setFullName(e.target.value)}
                  placeholder="Ada Lovelace"
                />
              </div>
              <div className="field">
                <label htmlFor="auth-org">Organization Name</label>
                <input
                  id="auth-org"
                  data-testid="input-orgname"
                  className="input"
                  required
                  value={orgName}
                  onChange={(e) => setOrgName(e.target.value)}
                  placeholder="Acme Engineering"
                />
              </div>
            </>
          )}

          {mode !== "reset" && (
            <div className="field">
              <label htmlFor="auth-password">Password</label>
              <input
                id="auth-password"
                data-testid="input-password"
                className="input"
                type="password"
                required
                minLength={8}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </div>
          )}

          {mode === "reset" && resetToken && (
            <>
              <div className="field">
                <label htmlFor="reset-tok">Reset Token</label>
                <input
                  id="reset-tok"
                  className="input mono"
                  value={resetToken}
                  onChange={(e) => setResetToken(e.target.value)}
                />
              </div>
              <div className="field">
                <label htmlFor="reset-pw">New Password</label>
                <input
                  id="reset-pw"
                  className="input"
                  type="password"
                  minLength={8}
                  required
                  value={newPassword}
                  onChange={(e) => setNewPassword(e.target.value)}
                />
              </div>
            </>
          )}

          <button
            type="submit"
            className="btn btn-primary"
            style={{ width: "100%" }}
            disabled={busy}
            data-testid="btn-auth-submit"
          >
            {busy ? "Working..." : mode === "login" ? "Sign In" : mode === "register" ? "Create Workspace" : "Continue"}
          </button>
        </form>
      </div>
    </div>
  );
};
