// Step 1 — Energy System: the physical site, land parcels, and GIS map.
// Technologies, load types, and network topology moved to Steps 2 and 3.
import SiteDesigner from "./SiteDesigner.jsx";

export function Step2Site({ cfg, patch, step, go }) {
  return <SiteDesigner cfg={cfg} patch={patch} step={step} go={go} />;
}
