// NatPower Development Intelligence Platform — 6-step closed-loop sizing wizard.
// The visual language is the DIP mock UI; every solved number comes from the real
// optimiser via /api (see api.js). State lives here and flows down to each step.
import { useEffect, useRef, useState } from "react";
import { getHealth, uploadProfile, getProfileSummary } from "./lib/api.js";
import { sizingSignature, loadSession, saveSession } from "./lib/session.js";
import { Step1Project } from "./steps/Step1Project.jsx";
import { Step2Site } from "./steps/Step2Site.jsx";
import { StepNetworkDesign } from "./steps/StepNetworkDesign.jsx";
import { StepObjective } from "./steps/StepObjective.jsx";
import { Step4Sizing } from "./steps/Step4Sizing.jsx";
import { Step5Compare } from "./steps/Step5Compare.jsx";
import { Step6Decision } from "./steps/Step6Decision.jsx";

// Land-first order: design the system on the land (land + tech + topology emerges)
// → define consumer → objective → SIZE (Phase 1: physical sizing, the LP) →
// compare → decision.
const STEPS = [
  { t: "Site & Energy System",  s: "Land parcels & GIS map" },
  { t: "Consumer & Demand",     s: "Technologies, loads & profiles" },
  { t: "Network Design",        s: "Energy flow topology" },
  { t: "Strategy & Goals",      s: "Green energy targets & grid rules" },
  { t: "Equipment Sizing",      s: "AI simulation & optimisation" },
  { t: "Design Comparison",     s: "Trade-off analysis & curves" },
  { t: "Decision Pack",         s: "Executive & lender ready" },
];
export const SIZING_STEP = 4;   // index of the Size step (0-based)

const DEFAULT_CFG = {
  projectName: "Felixstowe Port — DC Colocation",
  location: "Felixstowe, UK",
  // Objective targets — the relevant one is used per derived topology (Objective step).
  btmObjective: "ssr",    // btm        → ssr | gc | both
  ssrTarget: 95,          // btm        → self-sufficiency %
  gcTarget: null,         // backup     → grid-connection MW (null ⇒ lowest achievable)
  firmnessTarget: 99,     // off_grid   → firmness %
  curtailmentTarget: 5,   // standalone → max curtailment %
  // Consumers — the single source of truth, chosen in Step 1, reflected in Step 2.
  loads: [{ id: "load-dc", load_type: "data_centre", name: "Data Centre", peak_mw: 24, baseline_mw: 14 }],
  topology: "btm",   // derived from the flow designer; btm | off_grid | standalone | backup
  tech: { solar: true, wind: false, bess: true },
  // Dispatch policy. priority: [] ⇒ engine's default merit order for the objective;
  // a non-empty ordered list overrides it. allowGridCharge: null ⇒ infer from
  // objective, true/false ⇒ force it (enables battery pre-charging from the grid).
  // model: "rule" (causal, no foresight) | "rolling" (rolling-horizon forecast, W);
  // horizonH/commitH configure the rolling window when model === "rolling".
  dispatch: { priority: [], allowGridCharge: null, model: "rule", horizonH: 24, commitH: 1 },
  // Separate land parcels per technology — solar and wind compete for land, so
  // each has its own buildable area → its own max nameplate (set in Step 2).
  parcels: { solar: { areaHa: 184, maxMw: 166 }, wind: { areaHa: 184, maxMw: 48 } },
};

export default function App() {
  const saved = loadSession();                    // restore a persisted project (or null)
  const [health, setHealth] = useState("checking");
  const [step, setStep] = useState(saved?.step ?? 0);
  const [cfg, setCfg] = useState(saved?.cfg ? { ...DEFAULT_CFG, ...saved.cfg } : DEFAULT_CFG);
  const [profile, setProfile] = useState(saved?.profile ?? null); // upload info {profile_path, ...}
  const [summary, setSummary] = useState(null);  // profile-summary (nameplates) — re-fetched
  const [result, setResult] = useState(saved?.result ?? null);    // /run output (physical sizing)
  const [flows, setFlows] = useState(null);       // forward-eval flows — not persisted (large)
  const [running, setRunning] = useState(false);
  // Auxiliary sweep results, lifted here so they survive step navigation (were
  // local to DesignScenarios / SplitOptimiser and lost the moment you left Step 4).
  const [designResult, setDesignResult] = useState(saved?.designResult ?? null);
  const [splitResult, setSplitResult] = useState(saved?.splitResult ?? null);
  const [override, setOverride] = useState(saved?.override ?? "");
  // Signature of the inputs that produced `result`, for staleness detection.
  const [resultSig, setResultSig] = useState(saved?.resultSig ?? null);
  // "simple" (PM / developer) hides expert controls; "expert" reveals them all.
  const [mode, setMode] = useState(saved?.mode ?? "simple");
  const [navOpen, setNavOpen] = useState(true);   // collapsible sidebar
  const [projectModalOpen, setProjectModalOpen] = useState(false); // project info modal
  const fileRef = useRef();

  useEffect(() => {
    let cancelled = false;
    async function checkHealth() {
      try {
        const h = await getHealth();
        if (cancelled) return;
        if (h.profile_cached) {
          setHealth("ok");
        } else {
          setHealth("warming");
          // Poll until the default profile is cached (startup warm-up thread)
          setTimeout(checkHealth, 2000);
        }
      } catch {
        if (!cancelled) setHealth("down");
      }
    }
    checkHealth();
    return () => { cancelled = true; };
  }, []);

  // Pull nameplates/annual stats for the active dataset (bundled or uploaded).
  useEffect(() => {
    getProfileSummary(profile?.profile_path).then(setSummary).catch(() => setSummary(null));
  }, [profile]);

  // Persist the session (debounced) so a refresh restores the project. `flows` is
  // omitted on purpose — large and re-derivable by re-running (see saveSession).
  useEffect(() => {
    const id = setTimeout(() => saveSession({
      step, cfg, profile, result, designResult, splitResult, override, resultSig, mode,
    }), 400);
    return () => clearTimeout(id);
  }, [step, cfg, profile, result, designResult, splitResult, override, resultSig, mode]);

  // Close modal on Escape key
  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") setProjectModalOpen(false); };
    if (projectModalOpen) window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [projectModalOpen]);

  const patch = (p) => setCfg((c) => ({ ...c, ...p }));

  async function onUpload(e) {
    const file = e.target.files?.[0];
    if (!file) return;
    try {
      setProfile(await uploadProfile(file));
      setResult(null); setFlows(null); setResultSig(null);
      setDesignResult(null); setSplitResult(null);
    } catch (err) {
      alert("Upload rejected: " + (err.message || err));
    }
  }

  // Staleness: does the shown result still match the current inputs? Stamp the
  // signature at run time (commitRun); compare it to the live one each render.
  const currentSig = sizingSignature(cfg, profile);
  const stale = !!result && !!resultSig && resultSig !== currentSig;
  const commitRun = (res) => { setResult(res); setResultSig(sizingSignature(cfg, profile)); };

  const solved = !!(result?.design || (result?.table && result?.table.length > 0));   // for the version bar

  if (health === "down") {
    return (
      <div className="content" style={{ maxWidth: 720 }}>
        <div className="banner err">
          Cannot reach the backend at <code>/api</code>. Start it first:
          <pre>uvicorn webapp.api:app --reload --port 8000</pre>
        </div>
      </div>
    );
  }

  const ctx = {
    cfg, patch, profile, onUpload,
    result, setResult,
    flows, setFlows,
    designResult, setDesignResult,
    splitResult, setSplitResult,
    override, setOverride,
    summary,
    resultSig, setResultSig,
    stale, solved,
    running, setRunning,
    mode, setMode,
    step, go: (target) => setStep(target),
    commitRun
  };

  return (
    <div className="app">
      <div className="appbar">
        <div className="logo">Nat<span>Power</span></div>
        <div className="prod">Giga Park Design Tool</div>
        <div className="right">
        <span>backend <span className={"pill " + (health === "ok" ? "ready" : health === "warming" ? "warn" : "down")}>
            {health === "warming" ? "warming up…" : health}</span></span>
          <div className="avatar">PR</div>
        </div>
      </div>

      <div className="version-bar">
        <div style={{ display: "flex", alignItems: "center", flexWrap: "wrap", gap: 12 }}>
          <button
            type="button"
            onClick={() => setProjectModalOpen(true)}
            title="Click to edit Project Name & Site Location"
            style={{
              display: "inline-flex", alignItems: "center", gap: 6,
              background: "#fff", border: "1px solid #cbd5e1",
              borderRadius: 8, padding: "5px 12px",
              cursor: "pointer", fontSize: 13, fontWeight: 700, color: "#0f172a",
              boxShadow: "0 1px 2px rgba(0,0,0,0.05)",
            }}
          >
            <span>📍 {cfg.projectName || "New Project"}</span>
            <span style={{ fontWeight: 500, color: "#64748b", fontSize: 12 }}>· {cfg.location || "No location"}</span>
            <span style={{ fontSize: 11, background: "#f1f5f9", color: "#475569", padding: "2px 6px", borderRadius: 4, marginLeft: 4 }}>✏️ Edit info</span>
          </button>
          <span>Dataset: <b>{profile?.filename || "bundled 8760"}</b></span>
          <span>Solved: <b style={{ color: stale ? "var(--orange)" : solved ? "var(--ok)" : "var(--grey)" }}>
            {stale ? "Stale — re-run" : solved ? "Yes" : "No"}</b></span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          {/* Simple = PM / developer view · Expert = every engine control visible */}
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <span style={{ fontSize: 12, color: "var(--grey)" }}>View</span>
            <div style={{ display: "flex", border: "1px solid #d6dde0", borderRadius: 6, overflow: "hidden" }}>
              {[["simple", "Simple"], ["expert", "Expert"]].map(([m, lab]) => (
                <button key={m} className="btn small" style={{ padding: "3px 12px", border: "none", borderRadius: 0,
                  background: mode === m ? "#15616d" : "#fff", color: mode === m ? "#fff" : "#333" }}
                  onClick={() => setMode(m)}>{lab}</button>
              ))}
            </div>
          </div>
          <button className="btn small" onClick={() => fileRef.current?.click()}>Upload profiles (.xlsx)</button>
          <input ref={fileRef} type="file" accept=".xlsx,.xls" hidden onChange={onUpload} />
        </div>
      </div>

      <div className="shell">
        {/* Collapsible sidebar */}
        <aside className="stepper" style={{
          width: navOpen ? 248 : 0,
          padding: navOpen ? "24px 16px" : 0,
          overflowY: navOpen ? "auto" : "hidden",
          overflowX: "hidden",
          transition: "width 0.2s ease, padding 0.2s ease",
          position: "sticky",
          top: 100,
          maxHeight: "calc(100vh - 100px)",
          alignSelf: "flex-start",
          flexShrink: 0,
        }}>
          {/* Project name badge at top */}
          {navOpen && (
            <div
              onClick={() => setProjectModalOpen(true)}
              title="Click to edit project info"
              style={{
                marginBottom: 20, padding: "8px 10px",
                background: "var(--teal-light)", borderRadius: 8,
                fontSize: 12.5, fontWeight: 700, color: "var(--teal-dark)",
                display: "flex", alignItems: "center", justifyContent: "space-between", gap: 6,
                cursor: "pointer",
              }}
            >
              <div style={{ display: "flex", alignItems: "center", gap: 6, overflow: "hidden" }}>
                <span>📍</span>
                <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {cfg.projectName || "New Project"}
                </span>
              </div>
              <span style={{ fontSize: 11, opacity: 0.8 }}>✏️</span>
            </div>
          )}
          {STEPS.map((s, i) => (
            <div className={"s" + (i === step ? " active" : "") + (i < step ? " done" : "")}
                 key={i} onClick={() => setStep(i)}>
              <div className="num">{i < step ? "✓" : i + 1}</div>
              <div className="lab"><b>{s.t}</b><small>{s.s}</small></div>
            </div>
          ))}
        </aside>

        {/* Sidebar collapse/expand tab */}
        <button
          type="button"
          onClick={() => setNavOpen((o) => !o)}
          title={navOpen ? "Collapse navigation" : "Expand navigation"}
          style={{
            position: "sticky", top: 120, alignSelf: "flex-start",
            zIndex: 30, flexShrink: 0,
            width: 18, height: 52,
            background: "#fff",
            border: "1px solid var(--line)",
            borderLeft: navOpen ? "none" : "1px solid var(--line)",
            borderRadius: navOpen ? "0 8px 8px 0" : "8px",
            cursor: "pointer",
            display: "flex", alignItems: "center", justifyContent: "center",
            fontSize: 12, color: "#94a3b8", fontWeight: 700,
            boxShadow: "2px 0 6px rgba(0,0,0,0.05)",
            transition: "border-radius 0.2s",
            padding: 0, lineHeight: 1,
            marginTop: 32,
          }}
        >{navOpen ? "‹" : "›"}</button>

        <div className="content" style={{
          padding: step === 0 || step === 2 ? 0 : undefined,
          display: step === 0 || step === 2 ? "flex" : "block",
          flexDirection: "column",
          overflow: step === 0 || step === 2 ? "hidden" : undefined,
        }}>
          {step === 0 && <Step2Site {...ctx} />}
          {step === 1 && <Step1Project {...ctx} />}
          {step === 2 && <StepNetworkDesign {...ctx} />}
          {step === 3 && <StepObjective {...ctx} />}
          {step === 4 && <Step4Sizing {...ctx} />}
          {step === 5 && <Step5Compare {...ctx} />}
          {step === 6 && <Step6Decision {...ctx} />}
        </div>
      </div>

      {/* Project & Site Configuration Modal */}
      {projectModalOpen && (
        <div style={{
          position: "fixed", inset: 0, zIndex: 9999,
          background: "rgba(15, 23, 42, 0.5)",
          backdropFilter: "blur(4px)", WebkitBackdropFilter: "blur(4px)",
          display: "flex", alignItems: "center", justifyContent: "center", padding: 20,
        }} onClick={(e) => { if (e.target === e.currentTarget) setProjectModalOpen(false); }}>
          <div style={{
            background: "#fff", border: "1px solid #e2e8ea", borderRadius: 16,
            width: "100%", maxWidth: 520, padding: 24,
            boxShadow: "0 20px 25px -5px rgba(0,0,0,0.1), 0 8px 10px -6px rgba(0,0,0,0.1)",
            display: "flex", flexDirection: "column", gap: 16,
          }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
              <div>
                <h3 style={{ margin: 0, fontSize: 18, color: "#0f172a" }}>Project &amp; Site Configuration</h3>
                <div style={{ fontSize: 13, color: "#64748b", marginTop: 4 }}>
                  Set once during project creation. These details are used across GIS maps and decision reports.
                </div>
              </div>
              <button type="button" onClick={() => setProjectModalOpen(false)} style={{
                background: "transparent", border: "none", fontSize: 20, color: "#94a3b8", cursor: "pointer", padding: 4,
              }}>✕</button>
            </div>

            <div>
              <label className="fld" style={{ marginTop: 8 }}>Project Name</label>
              <input type="text"
                placeholder="e.g. Felixstowe Port — DC Colocation"
                value={cfg.projectName || ""}
                onChange={(e) => patch({ projectName: e.target.value })}
                style={{ fontWeight: 600, fontSize: 14 }}
              />
            </div>

            <div>
              <label className="fld">Site Location &amp; Coordinates</label>
              <input type="text"
                placeholder="e.g. Felixstowe, UK or 51.96, 1.35"
                value={cfg.location || ""}
                onChange={(e) => patch({ location: e.target.value })}
                style={{ fontWeight: 600, fontSize: 14 }}
              />
            </div>

            <div style={{ display: "flex", justifyContent: "flex-end", gap: 10, marginTop: 8 }}>
              <button type="button" className="btn" onClick={() => setProjectModalOpen(false)} style={{
                background: "#15616d", color: "#fff", padding: "8px 20px", fontWeight: 600, border: "none", borderRadius: 8, cursor: "pointer",
              }}>Save &amp; Continue</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
