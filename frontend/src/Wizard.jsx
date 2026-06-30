// Walks the question tree from GET /wizard. Each answer either advances to the
// next question or lands on a scenario id, which it hands up via onLeaf().
import { useEffect, useState } from "react";
import { getWizard } from "./api.js";

export default function Wizard({ onLeaf }) {
  const [tree, setTree] = useState(null);
  const [path, setPath] = useState([]); // [{qid, optIndex}]
  const [err, setErr] = useState(null);

  useEffect(() => {
    getWizard().then(setTree).catch((e) => setErr(String(e)));
  }, []);

  // Recompute the visible chain of questions from the chosen path. Stops at the
  // first unanswered question or at a leaf.
  function chain() {
    if (!tree) return { questions: [], leaf: null };
    const out = [];
    let cur = tree.start;
    let leaf = null;
    for (let guard = 0; cur && guard < 25; guard++) {
      const q = tree.questions[cur];
      if (!q) break;
      const step = path.find((p) => p.qid === cur);
      out.push({ qid: cur, q, chosen: step ? step.optIndex : null });
      if (step == null) break;
      const opt = q.options[step.optIndex];
      if (opt.leaf) { leaf = opt.leaf; break; }
      cur = opt.next;
    }
    return { questions: out, leaf };
  }

  // Set the answer for `qid`, dropping any answers that came after it (changing
  // an earlier choice invalidates the downstream branch). Rebuild forward from
  // the start so the kept answers always form a valid chain.
  function choose(qid, optIndex) {
    const trimmed = [];
    let cur = tree.start;
    for (let g = 0; cur && g < 25; g++) {
      if (cur === qid) {
        trimmed.push({ qid, optIndex });
        break;
      }
      const step = path.find((p) => p.qid === cur);
      if (!step) break;
      trimmed.push(step);
      const opt = tree.questions[cur].options[step.optIndex];
      if (opt.leaf) break;
      cur = opt.next;
    }
    setPath(trimmed);
  }

  useEffect(() => {
    const { leaf } = chain();
    onLeaf(leaf || null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, tree]);

  if (err) return <div className="banner err">Wizard failed to load: {err}</div>;
  if (!tree) return <div className="subtle"><span className="spinner" /> loading wizard…</div>;

  const { questions, leaf } = chain();

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span className="subtle">Answer each question; the path narrows to one scenario.</span>
        {path.length > 0 && (
          <button className="ghost small" onClick={() => setPath([])}>↺ Start over</button>
        )}
      </div>

      {questions.map(({ qid, q, chosen }) => (
        <div className="wiz-q" key={qid}>
          <div className="qtext">{q.text}</div>
          <div className="opts">
            {q.options.map((opt, i) => (
              <button
                key={i}
                className={"opt" + (chosen === i ? " active" : "")}
                onClick={() => choose(qid, i)}
              >
                {opt.label}
                {opt.leaf && !opt.leaf.runnable && (
                  <span className="pill backlog" style={{ marginLeft: 8 }}>planned</span>
                )}
              </button>
            ))}
          </div>
        </div>
      ))}

      {leaf && (
        <div className="banner info" style={{ marginTop: 8 }}>
          ➡️ Maps to <strong>{leaf.scenario_id}</strong> — {leaf.name}
          {!leaf.runnable && <> — <em>not runnable yet: {leaf.needs}</em></>}
        </div>
      )}
    </div>
  );
}
