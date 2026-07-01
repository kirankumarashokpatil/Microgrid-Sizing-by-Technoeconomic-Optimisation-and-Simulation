// NatPower Development Intelligence Platform — 6-step closed-loop sizing wizard.
// The visual language is the DIP mock UI; every solved number comes from the real
// optimiser via /api (see api.js). State lives here and flows down to each step.
import { useEffect, useRef, useState } from "react";
import { getHealth, uploadProfile, getProfileSummary } from "./lib/api.js";
import { Step1Project } from "./steps/Step1Project.jsx";
import { Step2Site } from "./steps/Step2Site.jsx";
import { StepObjective } from "./steps/StepObjective.jsx";
import { Step3Resource } from "./steps/Step3Resource.jsx";
import { Step4Sizing } from "./steps/Step4Sizing.jsx";
import { Step5Compare } from "./steps/Step5Compare.jsx";
import { Step6Decision } from "./steps/Step6Decision.jsx";

// Smart order: define the consumer, design the system (topology emerges), THEN
// ask the objective that fits that topology, then economics, then optimise.
const STEPS = [
  { t: "Consumer & Load", s: "Who you power + demand" },
  { t: "Energy System", s: "Tech, land, flow topology" },
  { t: "Objective", s: "Target that fits the system" },
  { t: "Assumptions", s: "Economics" },
  { t: "Optimise", s: "Engine sizing" },
  { t: "Comparison", s: "Multi-objective" },
  { t: "Decision Pack", s: "IC & lender ready" },
];
export const SIZING_STEP = 4;   // index of the Optimise step (used by result guards)

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
  // Land parcel → available generation (set by the drawer in Step 2).
  parcel: { areaHa: 184, maxSolarMw: 166, maxWindMw: 48 },
  econ: {
    grid_cost_mwh: 150,
    grid_connection_cost_mw: 250000,
    cost_pv_mw: 700000,
    cost_wind_mw: 1300000,
    cost_bess_mw: 150000,
    cost_bess_mwh: 300000,
    nominal_discount_rate_pct: 8,
    inflation_rate_pct: 2.5,
    project_lifespan_years: 20,
    fixed_opex_per_mwh_year: 8000,
  },
};

export default function App() {
  const [health, setHealth] = useState("checking");
  const [step, setStep] = useState(0);
  const [cfg, setCfg] = useState(DEFAULT_CFG);
  const [profile, setProfile] = useState(null); // upload info {profile_path, ...}
  const [summary, setSummary] = useState(null);  // profile-summary (nameplates)
  const [result, setResult] = useState(null);    // /run output incl. economics
  const [flows, setFlows] = useState(null);       // forward-eval flows at rec size
  const [running, setRunning] = useState(false);
  const fileRef = useRef();

  useEffect(() => {
    getHealth().then(() => setHealth("ok")).catch(() => setHealth("down"));
  }, []);

  // Pull nameplates/annual stats for the active dataset (bundled or uploaded).
  useEffect(() => {
    getProfileSummary(profile?.profile_path).then(setSummary).catch(() => setSummary(null));
  }, [profile]);

  const patch = (p) => setCfg((c) => ({ ...c, ...p }));

  async function onUpload(e) {
    const file = e.target.files?.[0];
    if (!file) return;
    try {
      setProfile(await uploadProfile(file));
      setResult(null); setFlows(null);
    } catch (err) {
      alert("Upload rejected: " + (err.message || err));
    }
  }

  const solved = !!result?.economics?.recommended;   // for the version bar

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

  const ctx = { cfg, patch, profile, summary, result, setResult, flows, setFlows,
                running, setRunning, step, go: setStep };

  return (
    <>
      <div className="appbar">
        <div className="logo">Nat<span>Power</span></div>
        <div className="prod">Development Intelligence Platform</div>
        <div className="right">
          <span>backend <span className={"pill " + (health === "ok" ? "ready" : "down")}>{health}</span></span>
          <div className="avatar">PR</div>
        </div>
      </div>

      <div className="version-bar">
        <div style={{ display: "flex", alignItems: "center", flexWrap: "wrap" }}>
          <span className="proj" style={{ marginRight: 20 }}>📍 {cfg.projectName}</span>
          <span>Dataset: <b>{profile?.filename || "bundled 8760"}</b></span>
          <span>Solved: <b style={{ color: solved ? "var(--ok)" : "var(--grey)" }}>{solved ? "Yes" : "No"}</b></span>
        </div>
        <div>
          <button className="btn small" onClick={() => fileRef.current?.click()}>Upload profiles (.xlsx)</button>
          <input ref={fileRef} type="file" accept=".xlsx,.xls" hidden onChange={onUpload} />
        </div>
      </div>

      <div className="shell">
        <aside className="stepper">
          {STEPS.map((s, i) => (
            <div className={"s" + (i === step ? " active" : "") + (i < step ? " done" : "")}
                 key={i} onClick={() => setStep(i)}>
              <div className="num">{i < step ? "✓" : i + 1}</div>
              <div className="lab"><b>{s.t}</b><small>{s.s}</small></div>
            </div>
          ))}
        </aside>

        <div className="content">
          {step === 0 && <Step1Project {...ctx} />}
          {step === 1 && <Step2Site {...ctx} />}
          {step === 2 && <StepObjective {...ctx} />}
          {step === 3 && <Step3Resource {...ctx} />}
          {step === 4 && <Step4Sizing {...ctx} />}
          {step === 5 && <Step5Compare {...ctx} />}
          {step === 6 && <Step6Decision {...ctx} />}
        </div>
      </div>
    </>
  );
}
