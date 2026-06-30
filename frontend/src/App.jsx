import { useEffect, useRef, useState } from "react";
import { getHealth, getCoverage, getScenarios, uploadProfile } from "./api.js";
import Wizard from "./Wizard.jsx";
import ScenarioForm from "./ScenarioForm.jsx";

export default function App() {
  const [health, setHealth] = useState("checking");
  const [coverage, setCoverage] = useState(null);
  const [scenarios, setScenarios] = useState([]);
  const [profile, setProfile] = useState(null); // {profile_path, filename, ...}
  const [mode, setMode] = useState("wizard");
  const [leaf, setLeaf] = useState(null);        // from wizard
  const [picked, setPicked] = useState("");      // from direct picker
  const [onlyReady, setOnlyReady] = useState(true);
  const fileRef = useRef();

  useEffect(() => {
    getHealth().then(() => setHealth("ok")).catch(() => setHealth("down"));
    getCoverage().then(setCoverage).catch(() => {});
    getScenarios().then(setScenarios).catch(() => {});
  }, []);

  async function onUpload(e) {
    const file = e.target.files?.[0];
    if (!file) return;
    try {
      const info = await uploadProfile(file);
      setProfile(info);
    } catch (err) {
      alert("Upload rejected: " + (err.message || err));
    }
  }

  if (health === "down") {
    return (
      <div className="main">
        <div className="banner err">
          Cannot reach the backend at <code>/api</code>. Start it first:
          <pre>uvicorn webapp.api:app --reload --port 8000</pre>
        </div>
      </div>
    );
  }

  // The active scenario id: wizard leaf (if runnable) or the direct pick.
  const activeId =
    mode === "wizard"
      ? (leaf && leaf.runnable ? leaf.scenario_id : null)
      : (picked || null);

  const shown = onlyReady ? scenarios.filter((s) => s.ready) : scenarios;

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand"><span className="dot">⚡</span><h1>DIP Explorer</h1></div>
        <div className="subtle" style={{ marginBottom: 18 }}>Scenario sizing · behind-the-meter</div>

        <h3>Data source</h3>
        <div className="upload-zone" onClick={() => fileRef.current?.click()}>
          {profile ? `✓ ${profile.filename}` : "Click to upload profiles (.xlsx)"}
        </div>
        <input ref={fileRef} type="file" accept=".xlsx,.xls" hidden onChange={onUpload} />
        {profile ? (
          <div className="subtle" style={{ marginTop: 8 }}>
            {profile.n_timesteps?.toLocaleString()} steps · peak {profile.peak_load_mw?.toFixed?.(0)} MW
            <br /><button className="ghost small" style={{ marginTop: 6 }} onClick={() => setProfile(null)}>use bundled instead</button>
          </div>
        ) : (
          <div className="subtle" style={{ marginTop: 8 }}>Using bundled 8760 dataset.</div>
        )}

        <h3 style={{ marginTop: 26 }}>Registry coverage</h3>
        {coverage && (
          <div>
            <div className="metric-row">
              <div><div className="subtle">Total</div><div style={{ fontSize: 22, fontWeight: 700 }}>{coverage.total}</div></div>
              <div><div className="subtle">Ready</div><div style={{ fontSize: 22, fontWeight: 700, color: "var(--good)" }}>{coverage.ready}</div></div>
            </div>
            <div className="subtle" style={{ marginTop: 8 }}>
              PV-variable {coverage.needs_pv_variable} · consumer {coverage.needs_consumer_layer} · minor {coverage.needs_minor}
            </div>
          </div>
        )}
      </aside>

      <main className="main">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 18 }}>
          <div className="seg">
            <button className={mode === "wizard" ? "active" : ""} onClick={() => setMode("wizard")}>🧭 Guided wizard</button>
            <button className={mode === "direct" ? "active" : ""} onClick={() => setMode("direct")}>📋 Pick directly</button>
          </div>
          <span className="subtle">backend: <span className="pill ready">{health}</span></span>
        </div>

        {mode === "wizard" ? (
          <>
            <Wizard onLeaf={setLeaf} />
            {activeId && (
              <div style={{ marginTop: 18 }}>
                <ScenarioForm scenarioId={activeId} profilePath={profile?.profile_path} />
              </div>
            )}
          </>
        ) : (
          <>
            <div style={{ marginBottom: 14 }}>
              <label className="field" style={{ maxWidth: 520 }}>
                <span className="lab">Scenario</span>
                <select value={picked} onChange={(e) => setPicked(e.target.value)}>
                  <option value="">— choose —</option>
                  {shown.map((s) => (
                    <option key={s.id} value={s.id}>{s.id} — {s.name}{s.ready ? "" : "  ⏳"}</option>
                  ))}
                </select>
              </label>
              <label className="subtle" style={{ cursor: "pointer" }}>
                <input type="checkbox" checked={onlyReady} onChange={(e) => setOnlyReady(e.target.checked)} /> only runnable
              </label>
            </div>
            {activeId && <ScenarioForm scenarioId={activeId} profilePath={profile?.profile_path} />}
          </>
        )}
      </main>
    </div>
  );
}
