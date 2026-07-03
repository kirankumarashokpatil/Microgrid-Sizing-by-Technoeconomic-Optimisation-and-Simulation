// FlowDesigner.jsx — Canvas-First Energy Network Designer (v3)
// UI follows the Figma / Draw.io mental model:
//   • Toolbar (top): tool selection + component palette
//   • Canvas (centre, hero element): drag-place + connect nodes
//   • Inspector (right): appears only when something is selected
//   • Status bar (bottom): live topology signals
//
// All topology-signal logic and onChange / persistence contracts are unchanged.

import { useCallback, useEffect, useRef, useState } from "react";
import { LOAD_TYPES } from "../lib/loads.js";

/* ─── colour palette ─── */
const PALETTE = ["#e0922f","#1f8a8a","#2f8f5b","#7a6f9b","#3f7cac","#c2603a"];
const pc = (i) => PALETTE[i % PALETTE.length];

/* ─── node / flow metadata ──────────────────────────────────────────────────────────────
 * canSendTo encodes the PHYSICAL energy flow rules for a microgrid:
 *   Sources  (solar / wind): generate power → send to storage, loads, or grid
 *   BESS:                    bidirectional  → charge from grid/generation, discharge to loads/grid
 *   Grid:                    bidirectional  → import to loads or BESS; export receives from generators
 *   Consumer (load):         pure sink      → can only receive, never send
 *
 * These rules are enforced in THREE places:
 *   1. buildFlows()   — auto-generated topology on boot
 *   2. newFlowsFor()  — when a node is added via tech toggle
 *   3. connectNodes() — when the user manually draws a connection
 * ────────────────────────────────────────────────────────────── */
const META = {
  solar:    { label:"Solar",    icon:"☀️",  color:"#e0922f", canSendTo:["bess","consumer","grid"] },
  wind:     { label:"Wind",     icon:"🌬️", color:"#1f8a8a", canSendTo:["bess","consumer","grid"] },
  bess:     { label:"BESS",     icon:"🔋",  color:"#2f8f5b", canSendTo:["consumer","grid"] },
  //  Grid can IMPORT (send power) to loads AND to BESS (for grid-charging).
  //  Grid cannot send to solar/wind (those are generators, not loads).
  grid:     { label:"Grid",     icon:"⚡",  color:"#7a6f9b", canSendTo:["bess","consumer"] },
  //  Consumer is a pure sink — it can never be the source of a power flow.
  consumer: { label:"Consumer", icon:"🏭",  color:"#3f7cac", canSendTo:[] },
};

// Human-readable rejection reasons (shown in the UI toast)
const FLOW_REJECT_REASON = {
  self:     "Cannot connect a node to itself.",
  exists:   "This connection already exists.",
  illegal:  (sType, tType) => {
    if (sType === "consumer")
      return `❌ ${META.consumer.label} is a load — it can only receive power, never send it.`;
    if (tType === "solar" || tType === "wind")
      return `❌ ${META[tType]?.label} is a generator — it cannot receive power from another node.`;
    return `❌ ${META[sType]?.label || sType} → ${META[tType]?.label || tType} is not a valid energy flow.`;
  },
};

const DEF_PARAMS = {
  solar:    { rated_mw:80,  eff_pct:97 },
  wind:     { rated_mw:40,  eff_pct:95 },
  bess:     { rated_mw:30,  capacity_mwh:120 },
  grid:     { max_export_mw:50, max_import_mw:30, allow_export:true, allow_import:true },
  consumer: { load_type:"data_centre", peak_mw:24, baseline_mw:14 },
};

/* ─── canvas node dimensions ─── */
const NODE_W = 110, NODE_H = 52;

let _uid = 0;
const mkId = () => ++_uid;

/* ─── consumer helper ─── */
function consumerNode(load) {
  return {
    id: load.id, type: "consumer", name: load.name,
    params: { load_type: load.load_type, peak_mw: load.peak_mw, baseline_mw: load.baseline_mw },
    x: 0, y: 0,
  };
}

/* ─── topology signal derivation (unchanged logic) ─── */
function deriveTopoSignals(nodes, flows) {
  const hasSolar   = nodes.some(n => n.type==="solar");
  const hasWind    = nodes.some(n => n.type==="wind");
  const hasGrid    = nodes.some(n => n.type==="grid");
  const hasBess    = nodes.some(n => n.type==="bess");
  const hasSources = hasSolar || hasWind;
  const gridNode   = nodes.find(n => n.type==="grid");
  const sorted     = [...flows].sort((a,b)=>a.pri-b.pri);

  const gridImportActive = sorted.some(f => {
    const src = nodes.find(n=>n.id===f.srcId);
    const tgt = nodes.find(n=>n.id===f.tgtId);
    return src?.type==="grid" && tgt?.type==="consumer" && f.on;
  });
  const exportActive = sorted.some(f => {
    const tgt = nodes.find(n=>n.id===f.tgtId);
    return tgt?.type==="grid" && f.on;
  });

  const off_grid   = !hasGrid;
  const backup     = hasGrid && !hasSources;
  const standalone = hasGrid && hasSources && !gridImportActive && exportActive;
  const btm        = hasGrid && hasSources && gridImportActive;

  const derivedMode = off_grid ? "off_grid" : backup ? "backup" : standalone ? "standalone" : "btm";

  const pvMw   = nodes.filter(n=>n.type==="solar").reduce((s,n)=>s+(+n.params.rated_mw||0),0);
  const windMw = nodes.filter(n=>n.type==="wind" ).reduce((s,n)=>s+(+n.params.rated_mw||0),0);
  const bessMw = nodes.filter(n=>n.type==="bess" ).reduce((s,n)=>s+(+n.params.rated_mw||0),0);
  const bessMwh= nodes.filter(n=>n.type==="bess" ).reduce((s,n)=>s+(+n.params.capacity_mwh||0),0);
  const peakMw = nodes.filter(n=>n.type==="consumer").reduce((s,n)=>s+(+n.params.peak_mw||0),0);

  const consumers = nodes.filter(n=>n.type==="consumer").map(n=>({
    name: n.name, load_type: n.params.load_type,
    peak_mw: +n.params.peak_mw, baseline_mw: +n.params.baseline_mw,
  }));

  return {
    derived_topology: derivedMode,
    grid_available:   hasGrid && (gridImportActive || !hasSources),
    off_grid, standalone,
    has_sources: hasSources,
    has_bess:    hasBess,
    pv_mw:       pvMw   > 0 ? pvMw   : undefined,
    wind_mw:     windMw > 0 ? windMw : undefined,
    bess_mw:     bessMw > 0 ? bessMw : undefined,
    bess_mwh:    bessMwh > 0 ? bessMwh : undefined,
    peak_load_mw: peakMw,
    consumers,
    export_limit_mw: standalone && gridNode?.params?.max_export_mw
      ? +gridNode.params.max_export_mw : undefined,
  };
}

const TOPO_META = {
  btm:        { label:"Grid-connected BTM",    color:"#1f8a8a", bg:"#e6f3f3" },
  backup:     { label:"Backup (no generation)", color:"#7a6f9b", bg:"#f0eeff" },
  off_grid:   { label:"Off-grid",              color:"#2f8f5b", bg:"#eef8f2" },
  standalone: { label:"Standalone export",     color:"#e0922f", bg:"#fbf0df" },
};

/* ─── default auto-layout ─── */
function defaultPositions(nodes, canvasW=820, canvasH=480) {
  const srcs  = nodes.filter(n => n.type==="solar"||n.type==="wind");
  const bess  = nodes.filter(n => n.type==="bess");
  const loads = nodes.filter(n => n.type==="consumer");
  const grid  = nodes.filter(n => n.type==="grid");

  const place = (items, cx, totalH) => {
    const step = totalH / (items.length + 1);
    items.forEach((n, i) => { n.x = cx; n.y = step * (i + 1); });
  };

  place(srcs,  canvasW * 0.15, canvasH * 0.7);
  place(bess,  canvasW * 0.45, canvasH * 0.7);
  place(loads, canvasW * 0.80, canvasH * 0.7);
  grid.forEach((n, i) => {
    n.x = canvasW * (0.35 + i * 0.15);
    n.y = canvasH * 0.82;
  });
  return nodes;
}

/* ─── flow helpers ────────────────────────────────────────── */
// canConnect: central rule check — the single source of truth for validity
function canConnect(srcType, tgtType) {
  return (META[srcType]?.canSendTo || []).includes(tgtType);
}

function buildFlows(nodes) {
  const fl = [];
  nodes.forEach(src =>
    (META[src.type]?.canSendTo || []).forEach(tt =>
      nodes.filter(n=>n.type===tt).forEach(tgt =>
        fl.push({ id:`${src.id}>${tgt.id}`, srcId:src.id, tgtId:tgt.id, pri:fl.length+1, on:true, cap_mw:20 })
      )
    )
  );
  return fl;
}

// Validate and purge any flows that violate canSendTo rules.
// Used when rehydrating from saved state to strip historically persisted bad edges.
function sanitiseFlows(nodes, flows) {
  const nodeMap = Object.fromEntries(nodes.map(n => [n.id, n]));
  return flows.filter(f => {
    const sn = nodeMap[f.srcId], tn = nodeMap[f.tgtId];
    return sn && tn && canConnect(sn.type, tn.type);
  });
}

function newFlowsFor(node, nodes, flows) {
  const nf=[]; const np=()=>flows.length+nf.length+1;
  (META[node.type]?.canSendTo||[]).forEach(tt=>
    nodes.filter(n=>n.type===tt).forEach(tgt=>{
      const id=`${node.id}>${tgt.id}`;
      if(!flows.some(f=>f.id===id)) nf.push({id,srcId:node.id,tgtId:tgt.id,pri:np(),on:true,cap_mw:20});
    })
  );
  Object.entries(META).forEach(([st,m])=>{
    if(!(m.canSendTo||[]).includes(node.type)) return;
    nodes.filter(n=>n.type===st).forEach(src=>{
      const id=`${src.id}>${node.id}`;
      if(!flows.some(f=>f.id===id)&&!nf.some(f=>f.id===id))
        nf.push({id,srcId:src.id,tgtId:node.id,pri:np(),on:true,cap_mw:20});
    });
  });
  return nf;
}

function compact(flows) {
  return [...flows].sort((a,b)=>a.pri-b.pri).map((f,i)=>({...f,pri:i+1}));
}

function makeNode(type, existingNodes, opts={}) {
  const cnt = existingNodes.filter(n=>n.type===type).length + 1;
  const lt = opts.load_type || "data_centre";
  const ltMeta = LOAD_TYPES?.[lt] || LOAD_TYPES?.data_centre || { label: lt };
  const name = type==="consumer" ? (ltMeta.label || lt) : `${META[type].label} ${cnt}`;
  const params = { ...JSON.parse(JSON.stringify(DEF_PARAMS[type]||{})), ...opts };
  return { id:`${type}-${mkId()}`, type, name, params, x:0, y:0 };
}

/* ═══════════════════════════════════════════════════════════════════════════
 * PORT ASSIGNMENT SYSTEM
 * ═══════════════════════════════════════════════════════════════════════════
 *
 * Problem: if 3 connections leave the same face of a node they all share one
 * centre point → overlapping arrows, visual clutter.
 *
 * Solution: treat each face as a "bus" — spread connections evenly along the
 * face and sort them by the other node's position so the resulting wires
 * cross as little as possible (like a proper bus in an electrical schematic).
 *
 * API:
 *   getFace(node, angle)               → 'right'|'left'|'up'|'down'
 *   computePortAssignments(nodes, flows)
 *     → Map  flowId+'::src' | flowId+'::tgt'  →  {x, y, face}
 *   edgePath(srcPort, tgtPort)          → {d, mid}   (orthogonal polyline)
 * ═══════════════════════════════════════════════════════════════════════════ */

const DIR_VEC = { right:[1,0], left:[-1,0], down:[0,1], up:[0,-1] };
const STUB = 26;   // px stub before first bend
const R    = 7;    // px corner-rounding radius
const f    = (n) => Math.round(n * 10) / 10;  // 1-dp — keeps SVG tidy

// ── Which face of the box does this angle point toward? ──────────────────────
function getFace(node, angle) {
  const absA   = Math.abs(angle);
  const aspect = Math.atan2(NODE_H / 2, NODE_W / 2); // ~25° for 110×52 box
  if (absA <= aspect)           return 'right';
  if (absA >= Math.PI - aspect) return 'left';
  return angle > 0 ? 'down' : 'up';
}

// ── Compute the exact {x,y} of one port given its node, face, slot & total ──
function slotToPort(node, face, slot, total) {
  const HW = NODE_W / 2, HH = NODE_H / 2;
  // Spread slots evenly across 70% of the face length, centred on the face
  const faceLen  = (face === 'right' || face === 'left') ? NODE_H : NODE_W;
  const usable   = faceLen * 0.70;
  const margin   = (faceLen - usable) / 2;          // gap from each corner
  const t        = total === 1 ? 0.5 : slot / (total - 1);  // 0 … 1
  const offset   = margin + t * usable - faceLen / 2;       // from node centre
  if (face === 'right') return { x: node.x + HW,  y: node.y + offset, face };
  if (face === 'left')  return { x: node.x - HW,  y: node.y + offset, face };
  if (face === 'down')  return { x: node.x + offset, y: node.y + HH,  face };
  /* up */              return { x: node.x + offset, y: node.y - HH,  face };
}

// ── Global port-assignment pass ───────────────────────────────────────────────
// Returns  portMap[`${flowId}::src`]  and  portMap[`${flowId}::tgt`]
// Re-run every render (cheap, pure function of nodes+flows).
function computePortAssignments(nodes, flows) {
  const HW = NODE_W / 2, HH = NODE_H / 2;

  // Step 1 — determine which face each end of each flow uses.
  // Key: `${nodeId}::${face}`,  value: array of { flowId, end, otherNode }
  const groups = {};
  const addGroup = (key, entry) => {
    if (!groups[key]) groups[key] = [];
    groups[key].push(entry);
  };

  flows.forEach(flow => {
    const sn = nodes.find(n => n.id === flow.srcId);
    const tn = nodes.find(n => n.id === flow.tgtId);
    if (!sn || !tn) return;
    const angle    = Math.atan2(tn.y - sn.y, tn.x - sn.x);
    const revAngle = Math.atan2(sn.y - tn.y, sn.x - tn.x);
    const srcFace  = getFace(sn, angle);
    const tgtFace  = getFace(tn, revAngle);   // true reverse vector angle
    addGroup(`${sn.id}::${srcFace}`, { flowId: flow.id, end: 'src', other: tn, face: srcFace, node: sn });
    addGroup(`${tn.id}::${tgtFace}`, { flowId: flow.id, end: 'tgt', other: sn, face: tgtFace, node: tn });
  });

  // Step 2 — for every face-bus, sort entries by the other node's position
  // along the perpendicular axis (minimises crossing) then assign slots.
  const portMap = {};
  Object.values(groups).forEach(entries => {
    const { face } = entries[0];
    const isH = face === 'right' || face === 'left';
    // Sort so the wire going to the topmost / leftmost target gets the topmost / leftmost slot
    entries.sort((a, b) => isH ? a.other.y - b.other.y : a.other.x - b.other.x);
    entries.forEach((e, slot) => {
      portMap[`${e.flowId}::${e.end}`] = slotToPort(e.node, face, slot, entries.length);
    });
  });

  return portMap;
}

// ── Rounded polyline builder ──────────────────────────────────────────────────
function roundedPolyline(pts) {
  if (pts.length < 2) return '';
  let d = `M ${f(pts[0].x)} ${f(pts[0].y)}`;
  for (let i = 1; i < pts.length - 1; i++) {
    const prev = pts[i-1], curr = pts[i], next = pts[i+1];
    const len1 = Math.hypot(curr.x-prev.x, curr.y-prev.y);
    const len2 = Math.hypot(next.x-curr.x, next.y-curr.y);
    if (len1 < 0.5 || len2 < 0.5) { d += ` L ${f(curr.x)} ${f(curr.y)}`; continue; }
    const r  = Math.min(R, len1/2, len2/2);
    const t1 = r / len1, t2 = r / len2;
    const bx = curr.x - (curr.x-prev.x)*t1,  by = curr.y - (curr.y-prev.y)*t1;
    const cx = curr.x + (next.x-curr.x)*t2,  cy = curr.y + (next.y-curr.y)*t2;
    d += ` L ${f(bx)} ${f(by)} Q ${f(curr.x)} ${f(curr.y)} ${f(cx)} ${f(cy)}`;
  }
  d += ` L ${f(pts[pts.length-1].x)} ${f(pts[pts.length-1].y)}`;
  return d;
}

// ── Orthogonal path between two pre-assigned ports ───────────────────────────
// srcPort / tgtPort each come from computePortAssignments: {x, y, face}
function edgePath(srcPort, tgtPort) {
  const [sex, sey] = DIR_VEC[srcPort.face];
  const [tex, tey] = DIR_VEC[tgtPort.face];

  // Extend each port outward by STUB before the first turn
  const ax = srcPort.x + sex*STUB,  ay = srcPort.y + sey*STUB;
  const bx = tgtPort.x + tex*STUB,  by = tgtPort.y + tey*STUB;

  // Perpendicular axes?
  const srcH = sey === 0, tgtH = tey === 0;
  const mids = [];
  if (srcH === tgtH) {
    // Same axis (both H or both V) → Z-shape through a perpendicular mid-segment
    if (srcH) { const mx=(ax+bx)/2; mids.push({x:mx,y:ay},{x:mx,y:by}); }
    else       { const my=(ay+by)/2; mids.push({x:ax,y:my},{x:bx,y:my}); }
  } else {
    // Perpendicular → single L-corner
    mids.push(srcH ? {x:bx,y:ay} : {x:ax,y:by});
  }

  const raw = [{x:srcPort.x,y:srcPort.y},{x:ax,y:ay},...mids,{x:bx,y:by},{x:tgtPort.x,y:tgtPort.y}];
  const pts = raw.filter((p,i) => i===0 || Math.hypot(p.x-raw[i-1].x,p.y-raw[i-1].y)>0.5);

  // True midpoint along actual path length (for priority badge)
  const segs = pts.slice(1).map((p,i)=>({
    len:Math.hypot(p.x-pts[i].x,p.y-pts[i].y), p1:pts[i], p2:p }));
  const half = segs.reduce((s,sg)=>s+sg.len,0)/2;
  let rem=half, mid=pts[0];
  for (const sg of segs) {
    if (rem<=sg.len) { const t=rem/sg.len; mid={x:sg.p1.x+t*(sg.p2.x-sg.p1.x),y:sg.p1.y+t*(sg.p2.y-sg.p1.y)}; break; }
    rem-=sg.len;
  }

  return { d: roundedPolyline(pts), mid };
}

/* ─── NetworkCanvas SVG component ─── */
function NetworkCanvas({ nodes, flows, selectedId, tool, pendingPlace,
                         onSelectNode, onSelectFlow, onNodeDrag, onConnect,
                         onCanvasClick, canvasW, canvasH }) {
  const svgRef = useRef();
  const dragState = useRef(null);
  const connectState = useRef(null);
  const [connectLine, setConnectLine] = useState(null);

  const sortedFlows = [...flows].sort((a,b)=>a.pri-b.pri);

  function svgPt(e) {
    const rect = svgRef.current.getBoundingClientRect();
    return { x: e.clientX - rect.left, y: e.clientY - rect.top };
  }

  function onNodeMouseDown(e, nodeId) {
    e.stopPropagation();
    if (tool === "connect") {
      connectState.current = { srcId: nodeId };
      const nd = nodes.find(n=>n.id===nodeId);
      const pt = svgPt(e);
      // Start rubber-band from the node centre (will snap to nearest port as mouse moves)
      setConnectLine({ x1:nd.x, y1:nd.y, x2:pt.x, y2:pt.y });
      return;
    }
    const nd = nodes.find(n=>n.id===nodeId);
    dragState.current = { nodeId, ox:e.clientX - nd.x, oy:e.clientY - nd.y };
    onSelectNode(nodeId);
  }

  function onNodeMouseUp(e, nodeId) {
    e.stopPropagation();
    if (connectState.current && nodeId && nodeId !== connectState.current.srcId) {
      onConnect(connectState.current.srcId, nodeId);
    }
    connectState.current = null;
    dragState.current = null;
    setConnectLine(null);
  }

  function onSvgMouseMove(e) {
    if (connectState.current) {
      const pt = svgPt(e);
      const nd = nodes.find(n=>n.id===connectState.current.srcId);
      if (nd) {
        // Rubber-band: snap origin to whichever face points toward the cursor
        const angle = Math.atan2(pt.y - nd.y, pt.x - nd.x);
        const face  = getFace(nd, angle);
        const port  = slotToPort(nd, face, 0, 1); // centre of that face
        setConnectLine({ x1:port.x, y1:port.y, x2:pt.x, y2:pt.y });
      }
      return;
    }
    if (dragState.current) {
      const { nodeId, ox, oy } = dragState.current;
      onNodeDrag(nodeId, e.clientX - ox, e.clientY - oy);
    }
  }

  function onSvgMouseUp(e) {
    connectState.current = null;
    dragState.current = null;
    setConnectLine(null);
  }

  function onSvgClick(e) {
    if (e.target === svgRef.current || e.target.getAttribute && e.target.getAttribute("data-bg")) {
      onCanvasClick(e);
    }
  }

  const nodeEls = nodes.map(nd => {
    const isC = nd.type==="consumer";
    const ltMeta = isC ? (LOAD_TYPES?.[nd.params.load_type]||LOAD_TYPES?.data_centre||{}) : null;
    const m = META[nd.type] || {};
    const color = isC ? (ltMeta?.color||"#3f7cac") : m.color;
    const icon  = isC ? (ltMeta?.icon||"🏭")       : m.icon;
    const isSelected = nd.id === selectedId;
    const activeFlows = flows.filter(f=>f.on&&(f.srcId===nd.id||f.tgtId===nd.id)).length;

    const subText = nd.type==="consumer" ? `${nd.params.peak_mw} MW peak`
      : nd.type==="bess" ? "optimiser sized"
      : nd.type==="grid" ? (nd.params.allow_export?"↕ import+export":"↓ import only")
      : `${nd.params.rated_mw} MW`;

    return (
      <g key={nd.id}
         style={{cursor: tool==="connect" ? "crosshair" : "grab"}}
         onMouseDown={e=>onNodeMouseDown(e, nd.id)}
         onMouseUp={e=>onNodeMouseUp(e, nd.id)}>
        {isSelected && (
          <rect x={nd.x-NODE_W/2-5} y={nd.y-NODE_H/2-5}
                width={NODE_W+10} height={NODE_H+10} rx={14}
                fill="none" stroke={color} strokeWidth={2}
                strokeDasharray="5 3" opacity={0.4}/>
        )}
        <rect x={nd.x-NODE_W/2} y={nd.y-NODE_H/2} width={NODE_W} height={NODE_H} rx={9}
              fill={isSelected ? `${color}18` : "#ffffff"}
              stroke={isSelected ? color : "#dde4ea"}
              strokeWidth={isSelected ? 2 : 1.5}
              style={{filter:"drop-shadow(0 2px 8px rgba(0,0,0,0.07))"}}/>
        <rect x={nd.x-NODE_W/2+6} y={nd.y-NODE_H/2+8}
              width={30} height={30} rx={7} fill={`${color}18`}/>
        <text x={nd.x-NODE_W/2+21} y={nd.y-NODE_H/2+23}
              textAnchor="middle" dominantBaseline="middle" fontSize={15}>{icon}</text>
        <text x={nd.x-NODE_W/2+44} y={nd.y-4}
              fill={color} fontSize={9.5} fontWeight={700}
              fontFamily="'Segoe UI',sans-serif">{nd.name}</text>
        <text x={nd.x-NODE_W/2+44} y={nd.y+9}
              fill="#94a3b8" fontSize={8}
              fontFamily="'Segoe UI',sans-serif">{subText}</text>
        {activeFlows > 0 && (
          <circle cx={nd.x+NODE_W/2-7} cy={nd.y-NODE_H/2+7} r={5} fill={color}/>
        )}
      </g>
    );
  });

  // Pre-compute separate staggered port positions for every connection on every face.
  // This is the key anti-clutter step: each flow gets its own unique point on the node.
  const portMap = computePortAssignments(nodes, sortedFlows);

  const flowEls = sortedFlows.map((f, i) => {
    const sn = nodes.find(n=>n.id===f.srcId), tn = nodes.find(n=>n.id===f.tgtId);
    if (!sn||!tn) return null;
    const exportBlocked = tn?.type==="grid" && tn.params?.allow_export===false;
    const active = f.on && !exportBlocked;
    const col = active ? pc(i) : "#c5d0d4";
    const isSelected = f.id === selectedId;
    const markId = `arr-${f.id.replace(/[^a-z0-9]/gi,'-')}-${i}`;
    // Look up the pre-assigned separate port for each end of this flow
    const srcPort = portMap[`${f.id}::src`];
    const tgtPort = portMap[`${f.id}::tgt`];
    if (!srcPort || !tgtPort) return null;
    const { d, mid } = edgePath(srcPort, tgtPort);

    return (
      <g key={f.id} onClick={e=>{e.stopPropagation();onSelectFlow(f.id);}}
         style={{cursor:"pointer"}}>
        <defs>
          <marker id={markId} markerWidth={8} markerHeight={6} refX={7} refY={3} orient="auto">
            <polygon points="0 0,8 3,0 6" fill={active?col:"#c5d0d4"}/>
          </marker>
        </defs>
        {/* wide invisible hit area */}
        <path d={d} fill="none" stroke="transparent" strokeWidth={20}/>
        {/* selection glow */}
        {isSelected && <path d={d} fill="none" stroke={col} strokeWidth={8} opacity={0.12} strokeLinecap="round"/>}
        {/* main edge */}
        <path d={d} fill="none" stroke={col}
              strokeWidth={active ? 2.5 : 1.5}
              strokeDasharray={active ? "10 5" : "5 5"}
              strokeLinecap="round"
              opacity={active ? 1 : 0.35}
              markerEnd={`url(#${markId})`}
              style={active ? {animation:"fdfd 1.2s linear infinite"} : {}}/>
        {/* priority badge at true curve midpoint */}
        <circle cx={mid.x} cy={mid.y} r={11}
                fill={active ? col : "#f4f6f8"}
                stroke={active ? "none" : col}
                strokeWidth={1}
                opacity={active?0.93:0.7}
                style={{filter:"drop-shadow(0 1px 3px rgba(0,0,0,0.15))"}}/>
        <text x={mid.x} y={mid.y} textAnchor="middle" dominantBaseline="middle"
              fill={active?"#fff":col} fontSize={8.5} fontWeight={700}
              fontFamily="'Segoe UI',sans-serif">
          {exportBlocked?"✕":(i+1)}
        </text>
      </g>
    );
  });

  return (
    <svg ref={svgRef} width={canvasW} height={canvasH}
         style={{display:"block", userSelect:"none"}}
         onMouseMove={onSvgMouseMove}
         onMouseUp={onSvgMouseUp}
         onClick={onSvgClick}>
      <defs>
        <pattern id="dotgrid" width={26} height={26} patternUnits="userSpaceOnUse">
          <circle cx={1} cy={1} r={1} fill="#dde4ea" opacity={0.7}/>
        </pattern>
      </defs>
      <rect width={canvasW} height={canvasH} fill="#f8fafc" data-bg="1"/>
      <rect width={canvasW} height={canvasH} fill="url(#dotgrid)" data-bg="1"/>
      {flowEls}
      {connectLine && (
        <line x1={connectLine.x1} y1={connectLine.y1}
              x2={connectLine.x2} y2={connectLine.y2}
              stroke="#1f8a8a" strokeWidth={1.5} strokeDasharray="6 4" opacity={0.75}/>
      )}
      {nodeEls}
      {nodes.length === 0 && (
        <text x={canvasW/2} y={canvasH/2}
              textAnchor="middle" fill="#c5cfd6"
              fontSize={13} fontFamily="'Segoe UI',sans-serif">
          Click a component above to place it — drag to arrange
        </text>
      )}
    </svg>
  );
}

/* ─── Inspector Panel ─── */
function InspField({ label, value, nodeId, paramKey, onChange }) {
  return (
    <div>
      <div style={{fontSize:11,color:"#94a3b8",marginBottom:4,fontWeight:600,
                   letterSpacing:0.3,textTransform:"uppercase"}}>{label}</div>
      <input type="number" defaultValue={value} key={`${nodeId}-${paramKey}-${value}`}
        onChange={e=>onChange(+e.target.value)}
        style={{width:"100%",padding:"7px 10px",border:"1px solid #e2e8ea",
                borderRadius:7,fontSize:13,fontFamily:"inherit",color:"#2f3a41",
                outline:"none",transition:"border-color .15s"}}
        onFocus={e=>e.target.style.borderColor="#1f8a8a"}
        onBlur={e=>e.target.style.borderColor="#e2e8ea"}/>
    </div>
  );
}

function ToggleRow({ label, checked, onChange }) {
  return (
    <div style={{display:"flex",alignItems:"center",justifyContent:"space-between",
                 padding:"8px 0",borderBottom:"1px solid #f1f5f9"}}>
      <span style={{fontSize:12.5,color:"#475569"}}>{label}</span>
      <div style={{position:"relative",width:34,height:18,flexShrink:0,cursor:"pointer"}}
           onClick={()=>onChange(!checked)}>
        <div style={{position:"absolute",inset:0,borderRadius:10,
                     background:checked?"#1f8a8a":"#cbd5e1",transition:"background .2s"}}/>
        <div style={{position:"absolute",width:12,height:12,left:checked?18:3,top:3,
                     background:"#fff",borderRadius:"50%",transition:"left .2s"}}/>
      </div>
    </div>
  );
}

/* ──────────────────────────────────────────────────
 * INSPECTOR PANEL
 * ────────────────────────────────────────────────── */
function InspectorPanel({ nodes, flows, selectedId, onRename, onSetParam, onRemoveNode,
                          onSetFlowOn, onSetPriority }) {
  const selNode = nodes.find(n=>n.id===selectedId);
  const selFlow = flows.find(f=>f.id===selectedId);

  if (!selNode && !selFlow) {
    return (
      <div style={{padding:"24px 16px",color:"#94a3b8",textAlign:"center",flex:1,
                   display:"flex",flexDirection:"column",alignItems:"center",justifyContent:"center"}}>
        <div style={{fontSize:32,marginBottom:12}}>👆</div>
        <div style={{fontWeight:700,color:"#64748b",marginBottom:8,fontSize:13}}>Nothing selected</div>
        <div style={{fontSize:12,lineHeight:1.7,color:"#94a3b8"}}>
          Click a node or connection to view and edit its properties.
        </div>
      </div>
    );
  }

  if (selNode) {
    const isC = selNode.type==="consumer";
    const ltMeta = isC ? (LOAD_TYPES?.[selNode.params.load_type]||{}) : null;
    const m = META[selNode.type]||{};
    const color = isC ? (ltMeta?.color||"#3f7cac") : m.color;
    const icon  = isC ? (ltMeta?.icon||"🏭")       : m.icon;

    return (
      <div style={{padding:"14px",overflowY:"auto"}}>
        <div style={{display:"flex",alignItems:"center",gap:10,marginBottom:16}}>
          <div style={{width:38,height:38,borderRadius:9,background:`${color}18`,flexShrink:0,
                       display:"flex",alignItems:"center",justifyContent:"center",fontSize:18}}>
            {icon}
          </div>
          <div style={{flex:1,minWidth:0}}>
            <input defaultValue={selNode.name} key={selNode.id}
              onBlur={e=>onRename(selNode.id, e.target.value)}
              style={{width:"100%",border:"none",borderBottom:"2px solid transparent",
                      fontSize:14,fontWeight:700,color:"#2f3a41",background:"transparent",
                      outline:"none",padding:"1px 2px",transition:"border-color .15s"}}
              onFocus={e=>e.target.style.borderBottomColor=color}
              onBlur2={e=>e.target.style.borderBottomColor="transparent"}/>
            <div style={{fontSize:11,color:"#94a3b8",marginTop:1}}>{m.label}</div>
          </div>
        </div>

        <div style={{display:"flex",flexDirection:"column",gap:12}}>
          {(selNode.type==="solar"||selNode.type==="wind") && <>
            <InspField label="Rated MW" value={selNode.params.rated_mw}
              nodeId={selNode.id} paramKey="rated_mw"
              onChange={v=>onSetParam(selNode.id,"rated_mw",v)}/>
            <InspField label="Efficiency %" value={selNode.params.eff_pct}
              nodeId={selNode.id} paramKey="eff_pct"
              onChange={v=>onSetParam(selNode.id,"eff_pct",v)}/>
          </>}

          {selNode.type==="bess" && (
            <div style={{padding:"10px 12px",background:"#f0fdf4",border:"1px solid #bbf7d0",
                         borderRadius:8,fontSize:12,color:"#166534",lineHeight:1.7}}>
              🔋 BESS power and capacity are <strong>solved automatically</strong> by the
              optimisation engine in the Sizing step. Just connect it — the engine does the rest.
            </div>
          )}

          {selNode.type==="grid" && <>
            <InspField label="Export limit (MW)" value={selNode.params.max_export_mw}
              nodeId={selNode.id} paramKey="max_export_mw"
              onChange={v=>onSetParam(selNode.id,"max_export_mw",v)}/>
            <ToggleRow label="Import from grid"
              checked={selNode.params.allow_import!==false}
              onChange={v=>onSetParam(selNode.id,"allow_import",v)}/>
            <ToggleRow label="Export to grid"
              checked={selNode.params.allow_export!==false}
              onChange={v=>onSetParam(selNode.id,"allow_export",v)}/>
          </>}

          {selNode.type==="consumer" && (
            <div style={{padding:"10px 12px",background:"#eff6ff",border:"1px solid #bfdbfe",
                         borderRadius:8,fontSize:12,color:"#1e40af",lineHeight:1.7}}>
              Loads are defined in <strong>Step 1 — Consumer &amp; Load</strong>.<br/>
              Peak: <strong>{selNode.params.peak_mw} MW</strong> ·
              Base: <strong>{selNode.params.baseline_mw} MW</strong>
            </div>
          )}
        </div>

        {selNode.type!=="consumer" && selNode.type!=="grid" && (
          <button onClick={()=>onRemoveNode(selNode.id)}
            style={{marginTop:20,width:"100%",padding:"8px",border:"1px solid #fee2e2",
                    borderRadius:8,background:"#fff5f5",color:"#c2603a",cursor:"pointer",
                    fontSize:12.5,fontWeight:600,transition:"all .15s"}}
            onMouseOver={e=>e.currentTarget.style.background="#fee2e2"}
            onMouseOut={e=>e.currentTarget.style.background="#fff5f5"}>
            🗑 Remove {m.label}
          </button>
        )}
      </div>
    );
  }

  if (selFlow) {
    const sn = nodes.find(n=>n.id===selFlow.srcId);
    const tn = nodes.find(n=>n.id===selFlow.tgtId);
    if (!sn||!tn) return null;
    const exportBlocked = tn?.type==="grid" && tn.params?.allow_export===false;
    const active = selFlow.on && !exportBlocked;
    const sorted = [...flows].sort((a,b)=>a.pri-b.pri);
    const rank = sorted.findIndex(f=>f.id===selFlow.id); // 0-based

    return (
      <div style={{padding:"14px",overflowY:"auto",display:"flex",flexDirection:"column",gap:0}}>
        {/* Header */}
        <div style={{fontSize:11,fontWeight:700,letterSpacing:.5,color:"#94a3b8",
                     marginBottom:12,textTransform:"uppercase"}}>Connection</div>
        <div style={{display:"flex",alignItems:"center",gap:6,marginBottom:14,flexWrap:"wrap"}}>
          <span style={{fontSize:17}}>{META[sn.type]?.icon}</span>
          <span style={{fontSize:12.5,fontWeight:700,color:"#2f3a41"}}>{sn.name}</span>
          <span style={{color:"#94a3b8",fontWeight:700}}>→</span>
          <span style={{fontSize:17}}>{META[tn.type]?.icon||"🏭"}</span>
          <span style={{fontSize:12.5,fontWeight:700,color:"#2f3a41"}}>{tn.name}</span>
        </div>

        {/* Status toggle */}
        {exportBlocked ? (
          <div style={{padding:"10px",background:"#fdf2f0",border:"1px solid #f0c4b6",
                       borderRadius:7,fontSize:12,color:"#c2603a",lineHeight:1.6,marginBottom:12}}>
            ✗ Zero-export constraint active — this flow is blocked.
          </div>
        ) : (
          <div style={{marginBottom:14}}>
            <ToggleRow label={`Enabled`} checked={selFlow.on} onChange={v=>onSetFlowOn(selFlow.id,v)}/>
          </div>
        )}

        {/* ─── Dispatch Priority ─────────────────────────── */}
        <div style={{fontSize:11,fontWeight:700,letterSpacing:.5,color:"#94a3b8",
                     marginBottom:8,textTransform:"uppercase"}}>Dispatch Priority</div>
        <div style={{background:"#f8fafc",border:"1px solid #e2e8ea",borderRadius:9,
                     overflow:"hidden",marginBottom:14}}>
          {sorted.map((f, idx) => {
            const fs = nodes.find(n=>n.id===f.srcId);
            const ft = nodes.find(n=>n.id===f.tgtId);
            if (!fs||!ft) return null;
            const isThis = f.id===selFlow.id;
            const blocked = ft?.type==="grid" && ft.params?.allow_export===false;
            return (
              <div key={f.id}
                style={{
                  display:"flex",alignItems:"center",gap:6,
                  padding:"7px 9px",
                  background:isThis?"#e6f3f3":"transparent",
                  borderBottom:idx<sorted.length-1?"1px solid #f1f5f9":"none",
                  cursor:"pointer",transition:"background .12s",
                }}
                onClick={()=>!isThis&&onSetFlowOn&&undefined /* select this flow */}
              >
                {/* Priority number */}
                <span style={{
                  minWidth:20,height:20,borderRadius:"50%",flexShrink:0,
                  background:isThis?"#15616d":(f.on&&!blocked?"#e0e7ef":"#f1f5f9"),
                  color:isThis?"#fff":(f.on&&!blocked?"#475569":"#94a3b8"),
                  fontSize:9.5,fontWeight:700,display:"flex",alignItems:"center",justifyContent:"center",
                }}>{idx+1}</span>

                {/* Source → Target */}
                <span style={{flex:1,fontSize:11,color:isThis?"#15616d":"#475569",
                              fontWeight:isThis?700:500,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>
                  {META[fs.type]?.icon} {fs.name} → {META[ft.type]?.icon||"🏭"} {ft.name}
                </span>

                {/* Status dot */}
                <span style={{width:7,height:7,borderRadius:"50%",flexShrink:0,
                              background:f.on&&!blocked?"#22c55e":"#cbd5e1"}}/>
              </div>
            );
          })}
        </div>

        {/* ─── Move up / down buttons ─────────────────────── */}
        <div style={{display:"flex",gap:6,marginBottom:6}}>
          <button
            disabled={rank<=0}
            onClick={()=>onSetPriority(selFlow.id, "up")}
            title="Increase dispatch priority (run earlier)"
            style={{
              flex:1,padding:"7px 0",border:"1.5px solid",borderRadius:7,cursor:rank<=0?"default":"pointer",
              borderColor:rank<=0?"#e2e8ea":"#1f8a8a",
              background:rank<=0?"#f8fafc":"#e6f3f3",
              color:rank<=0?"#cbd5e1":"#15616d",
              fontWeight:700,fontSize:12.5,transition:"all .15s",
            }}>
            ↑ Higher priority
          </button>
          <button
            disabled={rank>=sorted.length-1}
            onClick={()=>onSetPriority(selFlow.id, "down")}
            title="Lower dispatch priority (run later)"
            style={{
              flex:1,padding:"7px 0",border:"1.5px solid",borderRadius:7,
              cursor:rank>=sorted.length-1?"default":"pointer",
              borderColor:rank>=sorted.length-1?"#e2e8ea":"#64748b",
              background:rank>=sorted.length-1?"#f8fafc":"#f8fafc",
              color:rank>=sorted.length-1?"#cbd5e1":"#475569",
              fontWeight:700,fontSize:12.5,transition:"all .15s",
            }}>
            ↓ Lower priority
          </button>
        </div>
        <div style={{fontSize:10.5,color:"#94a3b8",textAlign:"center",lineHeight:1.6,marginBottom:2}}>
          Priority #{rank+1} of {sorted.length} — lower number dispatches first.
        </div>
      </div>
    );
  }

  return null;
}

/* ─── Main component ─── */
export default function FlowDesigner({ cfg, onTopoChange }) {
  const canvasContainerRef = useRef();
  const [canvasSize, setCanvasSize] = useState({ w:820, h:480 });
  const [tool, setTool] = useState("pointer");
  const [pendingPlace, setPendingPlace] = useState(null);
  const [selectedId, setSelectedId] = useState(null);
  const [flowToast, setFlowToast] = useState(null); // {msg, ok} rejection feedback

  useEffect(() => {
    if (!canvasContainerRef.current) return;
    const ro = new ResizeObserver(entries => {
      const { width, height } = entries[0].contentRect;
      if (width > 100) setCanvasSize({ w:Math.round(width), h:Math.max(420,Math.round(height)) });
    });
    ro.observe(canvasContainerRef.current);
    return () => ro.disconnect();
  }, []);

  // ── boot
  const boot = useRef(null);
  if (!boot.current) {
    const techTypes = [];
    if (cfg.tech?.solar) techTypes.push("solar");
    if (cfg.tech?.wind)  techTypes.push("wind");
    if (cfg.tech?.bess)  techTypes.push("bess");
    techTypes.push("grid");

    if (cfg.flowState?.nodes?.length) {
      const ns = cfg.flowState.nodes;
      const ids = new Set(ns.map(n=>n.id));
      // Sanitise restored flows: purge any edges that violate physical energy flow rules
      let fl = sanitiseFlows(ns, (cfg.flowState.flows||[]).filter(f=>ids.has(f.srcId)&&ids.has(f.tgtId)));
      if (!fl.length) fl = buildFlows(ns);
      boot.current = { nodes:ns, flows:fl };
    } else {
      const ns = [];
      techTypes.forEach(t => ns.push(makeNode(t, ns)));
      (cfg.loads||[]).forEach(l => ns.push(consumerNode(l)));
      defaultPositions(ns, 820, 480);
      boot.current = { nodes:ns, flows:buildFlows(ns) };
    }
  }

  const [nodes, setNodes] = useState(boot.current.nodes);
  const [flows, setFlows] = useState(boot.current.flows);

  const notify = useCallback((n, fl) => {
    if (!onTopoChange) return;
    const sig = deriveTopoSignals(n, fl);
    onTopoChange({
      nodes: n.map(nd => ({ id:nd.id, type:nd.type, name:nd.name, ...nd.params })),
      flows: [...fl].sort((a,b)=>a.pri-b.pri).map((f,i) => ({
        priority:i+1,
        source:n.find(x=>x.id===f.srcId)?.name,
        target:n.find(x=>x.id===f.tgtId)?.name,
        enabled:f.on, cap_mw:f.cap_mw,
      })),
      signals: sig,
      _raw: { nodes:n, flows:fl },
    });
  }, [onTopoChange]);

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { notify(nodes, flows); }, []);

  // sync loads from Step 1
  useEffect(() => {
    const loads = cfg.loads||[];
    const wantIds = loads.map(l=>l.id);
    const consumers = nodes.filter(n=>n.type==="consumer");
    const curIds = consumers.map(n=>n.id);
    const removed = consumers.filter(n=>!wantIds.includes(n.id)).map(n=>n.id);
    const added   = loads.filter(l=>!curIds.includes(l.id));
    const paramsChanged = consumers.some(n=>{
      const l=loads.find(x=>x.id===n.id);
      return l&&(n.name!==l.name||n.params.peak_mw!==l.peak_mw||n.params.baseline_mw!==l.baseline_mw||n.params.load_type!==l.load_type);
    });
    if (!removed.length&&!added.length&&!paramsChanged) return;
    let kept = nodes.filter(n=>n.type!=="consumer"||wantIds.includes(n.id))
                    .map(n=>n.type==="consumer"?{...consumerNode(loads.find(l=>l.id===n.id)),x:n.x,y:n.y}:n);
    let fl = flows.filter(f=>!removed.includes(f.srcId)&&!removed.includes(f.tgtId));
    added.forEach(l=>{
      const node=consumerNode(l);
      node.x=canvasSize.w*0.78; node.y=canvasSize.h*(0.3+Math.random()*0.35);
      fl=[...fl,...newFlowsFor(node,kept,fl)]; kept=[...kept,node];
    });
    fl=compact(fl);
    setNodes(kept); setFlows(fl); notify(kept,fl);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cfg.loads]);

  // sync tech toggles
  useEffect(() => {
    const types = new Set([
      ...(cfg.tech?.solar?["solar"]:[]),
      ...(cfg.tech?.wind ?["wind"] :[]),
      ...(cfg.tech?.bess ?["bess"] :[]),
      "grid","consumer",
    ]);
    const removedIds = nodes.filter(n=>!types.has(n.type)&&n.type!=="consumer").map(n=>n.id);
    let kept = nodes.filter(n=>!removedIds.includes(n.id));
    let fl   = flows.filter(f=>!removedIds.includes(f.srcId)&&!removedIds.includes(f.tgtId));
    let changed = removedIds.length>0;
    types.forEach(t=>{
      if (t!=="consumer"&&!kept.some(n=>n.type===t)){
        const node=makeNode(t,kept);
        node.x=100+(kept.filter(n=>n.type===t).length*30);
        node.y=100+kept.length*55;
        fl=[...fl,...newFlowsFor(node,kept,fl)]; kept=[...kept,node]; changed=true;
      }
    });
    if (changed){ fl=compact(fl); setNodes(kept); setFlows(fl); notify(kept,fl); }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cfg.tech?.solar,cfg.tech?.wind,cfg.tech?.bess]);

  // actions
  const addNodeAt = useCallback((type, x, y) => {
    setNodes(prev => {
      const node = makeNode(type, prev);
      node.x = x; node.y = y;
      const next = [...prev, node];
      setFlows(fl => { const nf=compact([...fl,...newFlowsFor(node,prev,fl)]); notify(next,nf); return nf; });
      return next;
    });
    setSelectedId(null);
  }, [notify]);

  const removeNode = useCallback(id => {
    setNodes(prev => {
      const n = prev.find(x=>x.id===id);
      if (!n||(n.type==="consumer"&&prev.filter(x=>x.type==="consumer").length<=1)) return prev;
      const next = prev.filter(x=>x.id!==id);
      setFlows(fl=>{const nf=compact(fl.filter(f=>f.srcId!==id&&f.tgtId!==id));notify(next,nf);return nf;});
      return next;
    });
    setSelectedId(null);
  }, [notify]);

  const renameNode = useCallback((id, name) => {
    if (!name?.trim()) return;
    setNodes(prev=>{const next=prev.map(n=>n.id===id?{...n,name:name.trim()}:n);notify(next,flows);return next;});
  }, [flows,notify]);

  const setNodeParam = useCallback((id, key, val) => {
    setNodes(prev=>{const next=prev.map(n=>n.id===id?{...n,params:{...n.params,[key]:val}}:n);notify(next,flows);return next;});
  }, [flows,notify]);

  const dragNode = useCallback((id, x, y) => {
    setNodes(prev=>prev.map(n=>n.id===id?{...n,x,y}:n));
  }, []);

  const connectNodes = useCallback((srcId, tgtId) => {
    const sn = nodes.find(n=>n.id===srcId);
    const tn = nodes.find(n=>n.id===tgtId);
    if (!sn || !tn) return;

    // Self-loop guard
    if (srcId === tgtId) {
      setFlowToast({ msg: FLOW_REJECT_REASON.self, ok: false });
      setTimeout(() => setFlowToast(null), 2800); return;
    }
    // Duplicate guard
    const id = `${srcId}>${tgtId}`;
    if (flows.some(f=>f.id===id)) {
      setFlowToast({ msg: FLOW_REJECT_REASON.exists, ok: false });
      setTimeout(() => setFlowToast(null), 2800); return;
    }
    // Physical energy flow rule guard
    if (!canConnect(sn.type, tn.type)) {
      setFlowToast({ msg: FLOW_REJECT_REASON.illegal(sn.type, tn.type), ok: false });
      setTimeout(() => setFlowToast(null), 3200); return;
    }

    const nf = compact([...flows, { id, srcId, tgtId, pri:flows.length+1, on:true, cap_mw:20 }]);
    setFlows(nf); notify(nodes, nf);
    setFlowToast({ msg: `✓ Connected ${sn.name} → ${tn.name}`, ok: true });
    setTimeout(() => setFlowToast(null), 1800);
  }, [nodes, flows, notify]);

  const setPriority = useCallback((flowId, direction) => {
    setFlows(prev => {
      const sorted = [...prev].sort((a,b) => a.pri - b.pri);
      const idx = sorted.findIndex(f => f.id === flowId);
      if (idx < 0) return prev;
      const swapIdx = direction === "up" ? idx - 1 : idx + 1;
      if (swapIdx < 0 || swapIdx >= sorted.length) return prev;
      // Swap priorities between this flow and its neighbour
      const priA = sorted[idx].pri;
      const priB = sorted[swapIdx].pri;
      const next = prev.map(f => {
        if (f.id === sorted[idx].id)   return { ...f, pri: priB };
        if (f.id === sorted[swapIdx].id) return { ...f, pri: priA };
        return f;
      });
      notify(nodes, next);
      return next;
    });
  }, [nodes, notify]);

  const setFlowOn = useCallback((id,val) => {
    setFlows(prev=>{const next=prev.map(f=>f.id===id?{...f,on:val}:f);notify(nodes,next);return next;});
  }, [nodes,notify]);

  const handleCanvasClick = useCallback((e) => {
    if (pendingPlace && canvasContainerRef.current) {
      const rect = canvasContainerRef.current.getBoundingClientRect();
      addNodeAt(pendingPlace, e.clientX - rect.left, e.clientY - rect.top);
      setPendingPlace(null);
    } else {
      setSelectedId(null);
    }
  }, [pendingPlace,addNodeAt]);

  const sig = deriveTopoSignals(nodes,flows);
  const topoMeta = TOPO_META[sig.derived_topology]||TOPO_META.btm;
  const activeFlows = flows.filter(f=>f.on).length;

  const palette = [
    {type:"solar",label:"Solar",icon:"☀️",color:"#e0922f"},
    {type:"wind", label:"Wind", icon:"🌬️",color:"#1f8a8a"},
    {type:"bess", label:"BESS", icon:"🔋", color:"#2f8f5b"},
  ];

  return (
    <div style={{
      display:"flex",flexDirection:"column",
      border:"1px solid #e2e8ea",borderRadius:12,
      background:"#fff",overflow:"hidden",
      boxShadow:"0 2px 12px rgba(0,0,0,0.06)",
      marginTop:20,
    }}>
      {/* ── Toolbar ── */}
      <div style={{
        display:"flex",alignItems:"center",
        borderBottom:"1px solid #e2e8ea",
        background:"#fff",flexShrink:0,flexWrap:"wrap",
        minHeight:48,
      }}>
        {/* Tool selector */}
        <div style={{display:"flex",alignItems:"center",gap:2,padding:"6px 10px",
                     borderRight:"1px solid #e2e8ea",flexShrink:0}}>
          {[{id:"pointer",icon:"⬆",label:"Select"},{id:"connect",icon:"⚡",label:"Connect"}].map(tb=>(
            <button key={tb.id} title={tb.label}
              onClick={()=>{setTool(tb.id);setPendingPlace(null);}}
              style={{
                padding:"5px 10px",border:"none",borderRadius:6,cursor:"pointer",
                background:tool===tb.id?"#e6f3f3":"transparent",
                color:tool===tb.id?"#15616d":"#64748b",
                fontSize:12,fontWeight:600,transition:"all .15s",
                display:"flex",alignItems:"center",gap:5,
              }}>
              <span style={{fontSize:13}}>{tb.icon}</span>
              <span>{tb.label}</span>
            </button>
          ))}
        </div>

        {/* Component palette */}
        <div style={{display:"flex",alignItems:"center",gap:3,padding:"6px 10px",flex:1,flexWrap:"wrap"}}>
          <span style={{fontSize:10.5,fontWeight:700,color:"#94a3b8",
                        letterSpacing:.5,marginRight:4,textTransform:"uppercase",flexShrink:0}}>
            Add:
          </span>
          {palette.map(p=>{
            const active=pendingPlace===p.type;
            return (
              <button key={p.type}
                onClick={()=>{
                  setPendingPlace(active?null:p.type);
                  setTool("pointer");
                }}
                style={{
                  display:"flex",alignItems:"center",gap:5,
                  padding:"5px 11px",border:`1.5px solid ${active?p.color:"#e2e8ea"}`,
                  borderRadius:7,cursor:"pointer",
                  background:active?`${p.color}15`:"#f8fafc",
                  color:active?p.color:"#475569",
                  fontSize:12,fontWeight:600,transition:"all .15s",
                  boxShadow:active?`0 0 0 3px ${p.color}25`:"none",
                }}>
                {p.icon} {p.label}
              </button>
            );
          })}
        </div>

        {/* Topology badge */}
        <div style={{padding:"0 12px",flexShrink:0}}>
          <span style={{
            fontSize:11,fontWeight:700,padding:"4px 11px",borderRadius:20,
            background:topoMeta.bg,color:topoMeta.color,
            border:`1px solid ${topoMeta.color}40`,whiteSpace:"nowrap",
          }}>{topoMeta.label}</span>
        </div>
      </div>

      {/* Pending hint */}
      {pendingPlace && (
        <div style={{
          padding:"7px 14px",background:"#e6f3f3",borderBottom:"1px solid #bfe0e0",
          fontSize:12.5,color:"#15616d",fontWeight:600,flexShrink:0,
          display:"flex",alignItems:"center",gap:10,
        }}>
          <span>📍 Click on the canvas to place {META[pendingPlace].icon} {META[pendingPlace].label}</span>
          <button onClick={()=>setPendingPlace(null)}
            style={{marginLeft:"auto",border:"none",background:"none",color:"#1f8a8a",
                    cursor:"pointer",fontWeight:700,fontSize:13,padding:0}}>✕</button>
        </div>
      )}

      {tool==="connect" && !pendingPlace && (
        <div style={{
          padding:"7px 14px",background:"#fef9ee",borderBottom:"1px solid #fde68a",
          fontSize:12.5,color:"#92400e",fontWeight:600,flexShrink:0,
        }}>
          ⚡ Connect mode — click a source node, then click a target node to draw a connection
        </div>
      )}

      {/* ── Body: Canvas + Inspector ── */}
      <div style={{display:"flex",flex:1,minHeight:420,overflow:"hidden"}}>
        {/* Canvas */}
        <div ref={canvasContainerRef}
             style={{flex:1,overflow:"hidden",position:"relative",
                     cursor:pendingPlace||tool==="connect"?"crosshair":"default"}}
             onClick={pendingPlace?handleCanvasClick:undefined}>
          <NetworkCanvas
            nodes={nodes} flows={flows}
            selectedId={selectedId}
            tool={tool}
            pendingPlace={pendingPlace}
            onSelectNode={id=>{setSelectedId(id);setPendingPlace(null);}}
            onSelectFlow={id=>{setSelectedId(id);setPendingPlace(null);}}
            onNodeDrag={dragNode}
            onConnect={connectNodes}
            onCanvasClick={handleCanvasClick}
            canvasW={canvasSize.w}
            canvasH={canvasSize.h}/>
        </div>

        {/* Inspector */}
        <div style={{
          width:232,flexShrink:0,borderLeft:"1px solid #e2e8ea",
          background:"#fff",overflowY:"auto",display:"flex",flexDirection:"column",
        }}>
          <div style={{padding:"10px 14px",borderBottom:"1px solid #f1f5f9",flexShrink:0,
                       fontSize:10.5,fontWeight:700,letterSpacing:.5,
                       color:"#94a3b8",textTransform:"uppercase"}}>
            Inspector
          </div>
          <InspectorPanel
            nodes={nodes} flows={flows}
            selectedId={selectedId}
            onRename={renameNode}
            onSetParam={setNodeParam}
            onRemoveNode={removeNode}
            onSetFlowOn={setFlowOn}
            onSetPriority={setPriority}/>
        </div>
      </div>

      {/* ── Flow validation toast (overlays canvas bottom) ── */}
      {flowToast && (
        <div style={{
          position:"absolute", bottom:52, left:"50%", transform:"translateX(-50%)",
          background: flowToast.ok ? "#166534" : "#7f1d1d",
          color:"#fff", padding:"9px 20px", borderRadius:24,
          fontSize:12.5, fontWeight:600, pointerEvents:"none",
          boxShadow:"0 4px 16px rgba(0,0,0,0.22)",
          animation:"fadeIn .15s ease",
          whiteSpace:"nowrap", zIndex:99,
        }}>
          {flowToast.msg}
        </div>
      )}

      {/* ── Status bar ── */}
      <div style={{
        display:"flex",alignItems:"center",gap:12,flexWrap:"wrap",
        padding:"7px 14px",borderTop:"1px solid #e2e8ea",
        background:"#f8fafc",flexShrink:0,fontSize:11,
      }}>
        <span style={{color:"#64748b",fontWeight:600}}>
          {nodes.length} nodes · {flows.length} connections · {activeFlows} active
        </span>
        {sig.pv_mw!=null&&<StatusChip label={`☀️ ${sig.pv_mw} MW`}/>}
        {sig.wind_mw!=null&&<StatusChip label={`🌬️ ${sig.wind_mw} MW`}/>}
        {sig.has_bess&&<StatusChip label="🔋 BESS"/>}
        {sig.grid_available&&<StatusChip label="⚡ Grid"/>}
        <StatusChip label={`🏭 ${sig.peak_load_mw} MW load`}/>
        <div style={{marginLeft:"auto",color:"#94a3b8",fontSize:10.5}}>
          {tool==="connect"
            ? "Click source → click target to connect"
            : pendingPlace
            ? `Click to place ${META[pendingPlace].label}`
            : "Click to select · Drag to reposition"}
        </div>
      </div>
    </div>
  );
}

function StatusChip({ label }) {
  return (
    <span style={{
      padding:"2px 9px",borderRadius:20,background:"#fff",
      border:"1px solid #e2e8ea",color:"#475569",whiteSpace:"nowrap",fontSize:11,
    }}>{label}</span>
  );
}
