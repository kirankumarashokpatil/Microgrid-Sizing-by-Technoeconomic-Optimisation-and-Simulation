// Small pieces shared across step screens: the footer navigation, and the guard
// shown on Steps 5–6 before the engine has produced a result.

export function Nav({ go, step, nextLabel = "Continue →", onNext, nextDisabled }) {
  return (
    <div className="navbtns">
      <button className="btn" disabled={step === 0} onClick={() => go(step - 1)}>Back</button>
      <button className="btn primary" disabled={nextDisabled}
              onClick={() => (onNext ? onNext() : go(step + 1))}>{nextLabel}</button>
    </div>
  );
}

export function NeedRun({ go, ranInfeasible }) {
  return (
    <div className="card">
      <div className={"banner " + (ranInfeasible ? "err" : "warn")}>
        {ranInfeasible
          ? <>The last run found <b>no feasible design</b>. Go back to Optimise and relax the target, enlarge the parcel, or add a grid connection.</>
          : <>No solved result yet. Go to the <b>Optimise</b> step and run the optimisation first.</>}
      </div>
      <button className="btn primary" style={{ marginTop: 14 }} onClick={() => go(4)}>← Back to Optimise</button>
    </div>
  );
}
