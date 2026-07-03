// FlowDesigner.jsx — Closed-Loop Energy Flow Designer (v2)
// Extended from the DIP mock UI FD engine with:
//   • Multiple named load types (DC, Port, EV, Industrial, HVAC, Generic)
//   • Multi-consumer support (add / remove consumers)
//   • Auto-derived topology signals sent to backend via onChange
//   • Live topology badge (BTM / Backup / Off-grid / Standalone)

import { useCallback, useEffect, useRef, useState } from "react";
import { LOAD_TYPES } from "../lib/loads.js";

/* ── colour palette ── */
const PC = ["#e0922f","#1f8a8a","#2f8f5b","#7a6f9b","#3f7cac","#c2603a","#15616d","#5f5680","#e0922f","#1f8a8a"];
const pc = (i) => PC[i % PC.length];

// A consumer node built from a Step-1 load (load.id is the stable node id, so the
// designer's consumers stay in sync with the loads chosen in Step 1).
function consumerNode(load) {
  return {
    id: load.id, type: "consumer", name: load.name,
    params: { load_type: load.load_type, peak_mw: load.peak_mw, baseline_mw: load.baseline_mw },
  };
}

/* ── node metadata (sources + storage + grid) ── */
const META = {
  solar:    { label:"Solar",    icon:"☀️",  color:"#e0922f", canSendTo:["bess","consumer","grid"] },
  wind:     { label:"Wind",     icon:"🌬️", color:"#1f8a8a", canSendTo:["bess","consumer","grid"] },
  bess:     { label:"BESS",     icon:"🔋",  color:"#2f8f5b", canSendTo:["consumer","grid"] },
  grid:     { label:"Grid",     icon:"⚡",  color:"#7a6f9b", canSendTo:["consumer"] },
  consumer: { label:"Consumer", icon:"🏭",  color:"#3f7cac", canSendTo:[] },
};

const DEF_PARAMS = {
  solar:    { rated_mw:80,  eff_pct:97 },
  wind:     { rated_mw:40,  eff_pct:95 },
  bess:     { rated_mw:30,  capacity_mwh:120 },
  grid:     { max_export_mw:50, max_import_mw:30, allow_export:true, allow_import:true },
  consumer: { load_type:"data_centre", peak_mw:24, baseline_mw:14 },
};

const PF = {
  solar:    [{ k:"rated_mw", lb:"MW" },{ k:"eff_pct", lb:"Eff %" }],
  wind:     [{ k:"rated_mw", lb:"MW" },{ k:"eff_pct", lb:"Eff %" }],
  bess:     [],   // BESS power/energy are SOLVED by the engine, not entered here
  grid:     [{ k:"max_export_mw", lb:"Export limit MW" }],
};

/* ── SVG layout constants ── */
const SW=720, SH=280, PAD=35, COL={src:70,sto:250,snk:642}, NW=43, NH=18, GRIDY=SH-30;

let _uid = 0;
const mkId = () => ++_uid;

/* ── topology signal derivation ── */
function deriveTopoSignals(nodes, flows) {
  const hasSolar   = nodes.some(n => n.type==="solar");
  const hasWind    = nodes.some(n => n.type==="wind");
  const hasGrid    = nodes.some(n => n.type==="grid");
  const hasBess    = nodes.some(n => n.type==="bess");
  const hasSources = hasSolar || hasWind;
  const gridNode   = nodes.find(n => n.type==="grid");
  const sorted     = [...flows].sort((a,b)=>a.pri-b.pri);

  // Grid → consumer flow present and enabled?
  const gridImportActive = sorted.some(f => {
    const src = nodes.find(n=>n.id===f.srcId);
    const tgt = nodes.find(n=>n.id===f.tgtId);
    return src?.type==="grid" && tgt?.type==="consumer" && f.on;
  });

  // Source → grid export flow present?
  const exportActive = sorted.some(f => {
    const src = nodes.find(n=>n.id===f.srcId);
    const tgt = nodes.find(n=>n.id===f.tgtId);
    return tgt?.type==="grid" && f.on;
  });

  const off_grid   = !hasGrid;
  const backup     = hasGrid && !hasSources;
  const standalone = hasGrid && hasSources && !gridImportActive && exportActive;
  const btm        = hasGrid && hasSources && gridImportActive;

  const derivedMode = off_grid ? "off_grid"
                    : backup   ? "backup"
                    : standalone ? "standalone"
                    : "btm";

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
    off_grid,
    standalone,
    has_sources: hasSources,
    has_bess:    hasBess,
    pv_mw:       pvMw > 0 ? pvMw : undefined,
    wind_mw:     windMw > 0 ? windMw : undefined,
    bess_mw:     bessMw > 0 ? bessMw : undefined,
    bess_mwh:    bessMwh > 0 ? bessMwh : undefined,
    peak_load_mw: peakMw,
    consumers,
    export_limit_mw: standalone && gridNode?.params?.max_export_mw
      ? +gridNode.params.max_export_mw : undefined,
  };
}

/* ── topology badge label/colour ── */
const TOPO_META = {
  btm:        { label:"Grid-connected BTM",    color:"#1f8a8a", bg:"#e6f3f3" },
  backup:     { label:"Backup (no generation)", color:"#7a6f9b", bg:"#f0eeff" },
  off_grid:   { label:"Off-grid",              color:"#2f8f5b", bg:"#eef8f2" },
  standalone: { label:"Standalone export",     color:"#e0922f", bg:"#fbf0df" },
};

/* ── node factory ── */
function makeNode(type, existingNodes, opts={}) {
  const cnt = existingNodes.filter(n=>n.type===type).length + 1;
  const lt = opts.load_type || "data_centre";
  const ltMeta = LOAD_TYPES[lt] || LOAD_TYPES.data_centre;
  const name = type==="consumer"
    ? ltMeta.label
    : `${META[type].label} ${cnt}`;
  const params = { ...JSON.parse(JSON.stringify(DEF_PARAMS[type])), ...opts };
  return { id:`${type}-${mkId()}`, type, name, params };
}

function buildNodes(types) {
  const nodes = [];
  types.forEach(t => nodes.push(makeNode(t, nodes)));
  return nodes;
}

function buildFlows(nodes) {
  const fl = [];
  nodes.forEach(src =>
    META[src.type].canSendTo.forEach(tt =>
      nodes.filter(n=>n.type===tt).forEach(tgt =>
        fl.push({ id:`${src.id}>${tgt.id}`, srcId:src.id, tgtId:tgt.id, pri:fl.length+1, on:true, cap_mw:20 })
      )
    )
  );
  return fl;
}

function newFlows(node, nodes, flows) {
  const nf=[]; const np=()=>flows.length+nf.length+1;
  META[node.type].canSendTo.forEach(tt=>
    nodes.filter(n=>n.type===tt).forEach(tgt=>{
      const id=`${node.id}>${tgt.id}`;
      if(!flows.some(f=>f.id===id)) nf.push({id,srcId:node.id,tgtId:tgt.id,pri:np(),on:true,cap_mw:20});
    })
  );
  Object.entries(META).forEach(([st,m])=>{
    if(!m.canSendTo.includes(node.type)) return;
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

/* ── SVG layout ── */
function colY(count, top=PAD, bot=SH-PAD-32) {
  return Array.from({length:count},(_,i)=>top+(bot-top)*(i+0.5)/count);
}
function bezMid(sx,sy,c1x,c1y,c2x,c2y,tx,ty){
  return {x:sx/8+3*c1x/8+3*c2x/8+tx/8, y:sy/8+3*c1y/8+3*c2y/8+ty/8};
}
function pInfo(sn,tn,bi=0){
  let sx,sy,tx,ty,c1x,c1y,c2x,c2y;
  if(tn.type==="grid"){
    sx=sn._pos.cx; sy=sn._pos.cy+NH; tx=tn._pos.cx; ty=tn._pos.cy-NH;
    const my=(sy+ty)/2; c1x=sx; c1y=my+16; c2x=tx; c2y=my-6;
  } else if(sn.type==="grid"){
    sx=sn._pos.cx; sy=sn._pos.cy-NH; tx=tn._pos.cx-NW; ty=tn._pos.cy;
    c1x=sx; c1y=sy-36; c2x=(sx+tx)/2; c2y=ty;
  } else if((sn.type==="solar"||sn.type==="wind")&&tn.type==="consumer"){
    sx=sn._pos.cx+NW; sy=sn._pos.cy; tx=tn._pos.cx-NW; ty=tn._pos.cy;
    const mx=(sx+tx)/2, by=PAD+4-bi*15; c1x=mx; c1y=by; c2x=mx; c2y=by;
  } else {
    sx=sn._pos.cx+NW; sy=sn._pos.cy; tx=tn._pos.cx-NW; ty=tn._pos.cy;
    const mx=(sx+tx)/2; c1x=mx; c1y=sy; c2x=mx; c2y=ty;
  }
  return {d:`M ${sx} ${sy} C ${c1x} ${c1y} ${c2x} ${c2y} ${tx} ${ty}`,mid:bezMid(sx,sy,c1x,c1y,c2x,c2y,tx,ty)};
}

function computePositions(nodes) {
  const srcs=nodes.filter(n=>n.type==="solar"||n.type==="wind");
  const stor=nodes.filter(n=>n.type==="bess");
  const grds=nodes.filter(n=>n.type==="grid");
  const snks=nodes.filter(n=>n.type==="consumer");
  const TOP=PAD, BOT=SH-PAD-32;
  const sy=colY(srcs.length,TOP,BOT), ry=colY(stor.length,TOP,BOT), ky=colY(snks.length,TOP,BOT);
  const out=nodes.map(n=>({...n,_pos:null}));
  srcs.forEach((n,i)=>{ out.find(x=>x.id===n.id)._pos={cx:COL.src,cy:sy[i]}; });
  stor.forEach((n,i)=>{ out.find(x=>x.id===n.id)._pos={cx:COL.sto,cy:ry[i]}; });
  snks.forEach((n,i)=>{ out.find(x=>x.id===n.id)._pos={cx:COL.snk,cy:ky[i]}; });
  const gL=COL.sto+95, gR=COL.snk-95;
  grds.forEach((n,i)=>{
    const gx=grds.length<2?(gL+gR)/2:gL+(gR-gL)*i/(grds.length-1);
    out.find(x=>x.id===n.id)._pos={cx:gx,cy:GRIDY};
  });
  return out;
}

/* ── SVG Diagram ── */
function DiagramSVG({nodes, flows, onToggleFlow}) {
  const sorted=[...flows].sort((a,b)=>a.pri-b.pri);
  const srcs=nodes.filter(n=>n.type==="solar"||n.type==="wind");
  const stor=nodes.filter(n=>n.type==="bess");
  const snks=nodes.filter(n=>n.type==="consumer");
  const grds=nodes.filter(n=>n.type==="grid");
  const gcx=grds.length?grds.reduce((a,n)=>a+n._pos.cx,0)/grds.length:0;

  if(!nodes.length) return (
    <svg width="100%" height="100%" viewBox={`0 0 ${SW} ${SH}`} preserveAspectRatio="xMidYMid meet" xmlns="http://www.w3.org/2000/svg" style={{display:"block"}}>
      <rect width={SW} height={SH} fill="#fcfdfd"/>
      <text x={SW/2} y={SH/2} textAnchor="middle" fill="#cdd6da" fontSize="13" fontFamily="'Segoe UI',sans-serif">Add nodes to build your flow diagram</text>
    </svg>
  );

  let topArc=0;
  const flowEls=sorted.map((f,i)=>{
    const sn=nodes.find(n=>n.id===f.srcId), tn=nodes.find(n=>n.id===f.tgtId);
    if(!sn?._pos||!tn?._pos) return null;
    const isBp=(sn.type==="solar"||sn.type==="wind")&&tn.type==="consumer";
    const bi=isBp?(topArc++):0;
    const {d,mid}=pInfo(sn,tn,bi);
    const exportBlocked=tn?.type==="grid"&&tn.params?.allow_export===false;
    const active=f.on&&!exportBlocked;
    const col=active?pc(i):"#c5d0d4", op=active?1:0.38;
    const mkId2=`fda-${f.id.replace(/[^a-z0-9]/gi,'-')}-${i}`;
    return (
      <g key={f.id} onClick={()=>!exportBlocked&&onToggleFlow(f.id)} style={{cursor:exportBlocked?"not-allowed":"pointer"}}>
        <defs><marker id={mkId2} markerWidth="7" markerHeight="5" refX="6" refY="2.5" orient="auto">
          <polygon points="0 0,7 2.5,0 5" fill={active?col:"#c5d0d4"}/></marker></defs>
        <path d={d} fill="none" stroke={col} strokeWidth="12" opacity={active?0.06:0}/>
        <path d={d} fill="none" stroke={col} strokeWidth={active?2:1.5} strokeDasharray="7 4" strokeLinecap="round"
              opacity={op} markerEnd={`url(#${mkId2})`} style={active?{animation:"fdfd 1.1s linear infinite"}:{}}/>
        <path d={d} fill="none" stroke="transparent" strokeWidth="20"/>
        <circle cx={mid.x} cy={mid.y} r="11" fill={active?col:"#f0f3f4"} stroke={col} strokeWidth={active?0:1} opacity={active?0.9:0.65}/>
        <text x={mid.x} y={mid.y} textAnchor="middle" dominantBaseline="middle"
              fill={active?"#fff":col} fontSize="9" fontWeight="700" fontFamily="'Segoe UI',sans-serif">
          {exportBlocked?"✕":(i+1)}
        </text>
      </g>
    );
  });

  const nodeEls=nodes.filter(n=>n._pos).map(n=>{
    const isC=n.type==="consumer";
    const ltMeta=isC?(LOAD_TYPES[n.params.load_type]||LOAD_TYPES.data_centre):null;
    const m=META[n.type];
    const nodeColor=isC?ltMeta.color:m.color;
    const nodeIcon=isC?ltMeta.icon:m.icon;
    const {cx,cy}=n._pos;
    const fc=sorted.filter(f=>f.on&&(f.srcId===n.id||f.tgtId===n.id)).length;
    const sub=n.type==="grid"?(n.params.allow_export?"↕ import + export":"↓ import only"):`${fc} flow${fc!==1?"s":""}`;
    const sw=isC?2:1.5;
    return (
      <g key={n.id}>
        <rect x={cx-NW} y={cy-NH} width={NW*2} height={NH*2} rx="6"
              fill={`${nodeColor}18`} stroke={nodeColor} strokeWidth={sw}/>
        <text x={cx-13} y={cy+1} textAnchor="middle" dominantBaseline="middle" fontSize="11">{nodeIcon}</text>
        <text x={cx+8} y={cy-4} textAnchor="middle" dominantBaseline="middle"
              fill={nodeColor} fontSize="9" fontWeight="700" fontFamily="'Segoe UI',sans-serif">{n.name}</text>
        <text x={cx+8} y={cy+6} textAnchor="middle" dominantBaseline="middle"
              fill={`${nodeColor}99`} fontSize="7.5" fontFamily="'Segoe UI',sans-serif">{sub}</text>
      </g>
    );
  });

  return (
    <svg width="100%" height="100%" viewBox={`0 0 ${SW} ${SH}`} preserveAspectRatio="xMidYMid meet" xmlns="http://www.w3.org/2000/svg" style={{display:"block"}}>
      <rect width={SW} height={SH} fill="#fcfdfd"/>
      {srcs.length>0&&<text x={COL.src} y="16" textAnchor="middle" fill="#bcc5ca" fontSize="8.5" fontFamily="'Segoe UI',sans-serif" fontWeight="700" letterSpacing=".5">SOURCES</text>}
      {stor.length>0&&<text x={COL.sto} y="16" textAnchor="middle" fill="#bcc5ca" fontSize="8.5" fontFamily="'Segoe UI',sans-serif" fontWeight="700" letterSpacing=".5">STORAGE</text>}
      {snks.length>0&&<text x={COL.snk} y="16" textAnchor="middle" fill="#3f7cac99" fontSize="8.5" fontFamily="'Segoe UI',sans-serif" fontWeight="700" letterSpacing=".5">CONSUMER(S)</text>}
      {grds.length>0&&<text x={gcx} y={GRIDY+NH+11} textAnchor="middle" fill="#7a6f9b99" fontSize="8.5" fontFamily="'Segoe UI',sans-serif" fontWeight="700" letterSpacing=".5">GRID (import / export)</text>}
      {flowEls}
      {nodeEls}
    </svg>
  );
}

/* ── Flow priority list with drag-to-reorder ── */
function FlowList({nodes, flows, onToggleFlow, onSetFlowOn, onSetFlowCap, onReorder}) {
  const sorted=[...flows].sort((a,b)=>a.pri-b.pri);
  const dragRef=useRef(null);
  if(!sorted.length) return (
    <div style={{textAlign:"center",color:"var(--grey)",padding:"10px",fontSize:"12px"}}>No flows — add nodes to generate routes</div>
  );
  return sorted.map((f,i)=>{
    const sn=nodes.find(n=>n.id===f.srcId), tn=nodes.find(n=>n.id===f.tgtId);
    if(!sn||!tn) return null;
    const exportBlocked=tn?.type==="grid"&&tn.params?.allow_export===false;
    const active=f.on&&!exportBlocked;
    const col=pc(i);
    const srcIcon=sn.type==="consumer"?(LOAD_TYPES[sn.params.load_type]||LOAD_TYPES.data_centre).icon:META[sn.type].icon;
    const tgtIcon=tn.type==="consumer"?(LOAD_TYPES[tn.params.load_type]||LOAD_TYPES.data_centre).icon:META[tn.type].icon;
    const srcLabel=sn.type==="consumer"?(LOAD_TYPES[sn.params.load_type]?.label||"Consumer"):META[sn.type].label;
    const tgtLabel=tn.type==="consumer"?(LOAD_TYPES[tn.params.load_type]?.label||"Consumer"):META[tn.type].label;
    return (
      <div key={f.id}
        className={"fd-flow-item"+(active?"":" fd-off")}
        draggable
        onDragStart={ev=>{dragRef.current=f.id;ev.currentTarget.style.opacity=".25";ev.dataTransfer.effectAllowed="move";}}
        onDragEnd={ev=>{ev.currentTarget.style.opacity="";ev.currentTarget.style.outline="";}}
        onDragOver={ev=>{ev.preventDefault();ev.currentTarget.style.outline="2px solid var(--teal)";ev.currentTarget.style.outlineOffset="-2px";}}
        onDragLeave={ev=>{ev.currentTarget.style.outline="";}}
        onDrop={ev=>{ev.preventDefault();ev.currentTarget.style.outline="";if(dragRef.current&&dragRef.current!==f.id)onReorder(dragRef.current,f.id);dragRef.current=null;}}>
        <span style={{color:"var(--grey)",cursor:"grab",fontSize:"13px"}}>⠿</span>
        <span style={{width:20,height:20,borderRadius:"50%",background:active?col:"#c5d0d4",color:"#fff",display:"flex",alignItems:"center",justifyContent:"center",fontSize:"10px",fontWeight:"700",flexShrink:0}}>
          {exportBlocked?"✕":(i+1)}
        </span>
        <div style={{flex:1,minWidth:0}}>
          <div style={{fontSize:"12px",fontWeight:500,color:"var(--slate)",whiteSpace:"nowrap",overflow:"hidden",textOverflow:"ellipsis"}}>{sn.name} → {tn.name}</div>
          <div style={{fontSize:"10.5px",color:"var(--grey)"}}>
            {srcIcon} {srcLabel} → {tgtIcon} {tgtLabel}
            {sn.type==="grid"&&<span style={{color:"var(--teal)",fontWeight:600,marginLeft:4}}>↓ grid import</span>}
            {exportBlocked&&<span style={{color:"var(--red)",fontWeight:600,marginLeft:5}}>✗ zero-export</span>}
          </div>
        </div>
        {exportBlocked
          ?<span style={{fontSize:"10.5px",color:"var(--red)",flexShrink:0,fontWeight:600}}>Blocked</span>
          :<label className="fd-tog">
              <input type="checkbox" checked={f.on} onChange={ev=>onSetFlowOn(f.id,ev.target.checked)}/>
              <span className="fd-tog-t"></span>
            </label>}
      </div>
    );
  });
}

/* ── Node Manager panel ── */
function NodePanel({nodes, onAdd, onRemove, onRename, onSetParam}) {
  const consumers = nodes.filter(n=>n.type==="consumer");
  const sourceTypes = ["solar","wind","bess","grid"];
  return (
    <div style={{overflowY:"auto",maxHeight:420}}>
      {/* Source / storage / grid blocks */}
      {sourceTypes.map(type=>{
        const m=META[type], nds=nodes.filter(n=>n.type===type);
        return (
          <div key={type} className="fd-node-block">
            <div className="fd-node-hd">
              <span>{m.icon}</span>
              <span style={{fontSize:"11.5px",fontWeight:600,color:m.color,flex:1}}>{m.label}</span>
              <span style={{fontSize:"10px",color:"var(--grey)"}}>{nds.length}</span>
            </div>
            {nds.length===0
              ?<div style={{padding:"5px 8px",fontSize:"10.5px",color:"var(--grey)",textAlign:"center"}}>None — click Add below</div>
              :nds.map(n=>(
                <div key={n.id} className="fd-node-row">
                  <div style={{display:"flex",alignItems:"center",gap:4,marginBottom:4}}>
                    <input defaultValue={n.name}
                      style={{flex:1,border:"none",borderBottom:"1px solid transparent",fontSize:"11.5px",fontWeight:500,color:"var(--slate)",background:"transparent",outline:"none",padding:"1px 2px"}}
                      onFocus={e=>e.target.style.borderBottomColor="var(--teal)"}
                      onBlur={e=>{e.target.style.borderBottomColor="transparent";onRename(n.id,e.target.value);}}/>
                    <button onClick={()=>onRemove(n.id)}
                      style={{border:"none",background:"none",color:"var(--grey)",cursor:"pointer",fontSize:"14px",padding:0,lineHeight:1}}
                      onMouseOver={e=>e.currentTarget.style.color="var(--red)"} onMouseOut={e=>e.currentTarget.style.color="var(--grey)"}>×</button>
                  </div>
                  <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:3}}>
                    {PF[type].map(field=>(
                      <div key={field.k}>
                        <div style={{fontSize:"9px",color:"var(--grey)",marginBottom:1}}>{field.lb}</div>
                        <input type="number" defaultValue={n.params[field.k]}
                          style={{width:"100%",padding:"2px 4px",fontSize:"10.5px",border:"1px solid var(--line2)",borderRadius:"3px"}}
                          onChange={ev=>onSetParam(n.id,field.k,+ev.target.value)}/>
                      </div>
                    ))}
                  </div>
                  {type==="grid"&&(
                    <div style={{display:"flex",gap:10,marginTop:6,paddingTop:5,borderTop:"1px solid var(--line)",fontSize:"11px",flexWrap:"wrap"}}>
                      <label style={{display:"flex",alignItems:"center",gap:5,flex:1,cursor:"pointer"}}>
                        <span style={{fontSize:"9.5px",color:"var(--grey)"}}>Import from grid</span>
                        <label className="fd-tog" style={{marginLeft:"auto"}}>
                          <input type="checkbox" checked={n.params.allow_import!==false}
                            onChange={ev=>onSetParam(n.id,"allow_import",ev.target.checked)}/>
                          <span className="fd-tog-t"></span>
                        </label>
                      </label>
                      <label style={{display:"flex",alignItems:"center",gap:5,flex:1,cursor:"pointer"}}>
                        <span style={{fontSize:"9.5px",color:"var(--grey)"}}>Export to grid</span>
                        <label className="fd-tog" style={{marginLeft:"auto"}}>
                          <input type="checkbox" checked={n.params.allow_export!==false}
                            onChange={ev=>onSetParam(n.id,"allow_export",ev.target.checked)}/>
                          <span className="fd-tog-t"></span>
                        </label>
                      </label>
                    </div>
                  )}
                </div>
              ))}
            <button className="fd-add-btn" onClick={()=>onAdd(type)}>＋ Add {m.label}</button>
          </div>
        );
      })}

      {/* Consumer / Load blocks — read-only; loads are defined in Step 1 */}
      <div className="fd-node-block">
        <div className="fd-node-hd">
          <span>🏭</span>
          <span style={{fontSize:"11.5px",fontWeight:600,color:"#3f7cac",flex:1}}>
            Loads <span style={{fontWeight:400,fontSize:"9px",color:"var(--grey)"}}>from Step 1</span>
          </span>
          <span style={{fontSize:"10px",color:"var(--grey)"}}>{consumers.length}</span>
        </div>
        {consumers.map(n=>{
          const lt=LOAD_TYPES[n.params.load_type]||LOAD_TYPES.generic;
          return (
            <div key={n.id} className="fd-node-row" style={{borderLeft:`3px solid ${lt.color}`,display:"flex",alignItems:"center",gap:6}}>
              <span>{lt.icon}</span>
              <span style={{flex:1,fontSize:"11.5px",fontWeight:500,color:"var(--slate)"}}>{n.name}</span>
              <span style={{fontSize:"10px",color:"var(--grey)"}}>{n.params.peak_mw} MW</span>
            </div>
          );
        })}
        <div style={{padding:"6px 8px",fontSize:"10px",color:"var(--grey)",textAlign:"center"}}>Edit loads in Step 1 — Project Definition</div>
      </div>
    </div>
  );
}

/* ── Main exported component ── */
export default function FlowDesigner({cfg, onTopoChange}) {
  // The non-consumer node types come from the tech toggles; consumers come from
  // the Step-1 loads (cfg.loads) — that's the Step 1 → Step 2 continuity.
  const techTypes = () => {
    const t=[];
    if(cfg.tech?.solar) t.push("solar");
    if(cfg.tech?.wind)  t.push("wind");
    if(cfg.tech?.bess)  t.push("bess");
    t.push("grid");
    return t;
  };
  const loadNodes = () => (cfg.loads || []).map(consumerNode);

  // Build nodes ONCE and derive flows from those SAME nodes, so flow srcId/tgtId
  // reference real node ids. (The old code called buildNodes() twice — each call
  // minted new ids — so flows pointed at phantom nodes: no edges drawn, "0 flows"
  // on every node, and grid→consumer flows invisible to the signal derivation.)
  // Rehydrate from cfg.flowState so the design survives step navigation.
  const boot = useRef(null);
  if (!boot.current) {
    if (cfg.flowState?.nodes?.length) {
      const ns = computePositions(cfg.flowState.nodes);
      const ids = new Set(ns.map(n => n.id));
      // Self-heal: drop any persisted flow that doesn't reference real nodes, and
      // rebuild from scratch if none survive (guards against stale/broken state).
      let fl = (cfg.flowState.flows || []).filter(f => ids.has(f.srcId) && ids.has(f.tgtId));
      if (!fl.length) fl = buildFlows(ns);
      boot.current = { nodes: ns, flows: fl };
    } else {
      const ns = [...buildNodes(techTypes()), ...loadNodes()];
      boot.current = { nodes: computePositions(ns), flows: buildFlows(ns) };
    }
  }
  const [nodes,setNodes]=useState(boot.current.nodes);
  const [flows,setFlows]=useState(boot.current.flows);

  // Sync consumer nodes to the Step-1 loads: add new loads, drop removed ones, and
  // update name/peak/base for existing — keeping topology + peak_load_mw in step.
  useEffect(()=>{
    const loads = cfg.loads || [];
    const wantIds = loads.map(l=>l.id);
    const consumers = nodes.filter(n=>n.type==="consumer");
    const curIds = consumers.map(n=>n.id);
    const removed = consumers.filter(n=>!wantIds.includes(n.id)).map(n=>n.id);
    const added   = loads.filter(l=>!curIds.includes(l.id));
    const paramsChanged = consumers.some(n=>{
      const l=loads.find(x=>x.id===n.id);
      return l && (n.name!==l.name || n.params.peak_mw!==l.peak_mw
                || n.params.baseline_mw!==l.baseline_mw || n.params.load_type!==l.load_type);
    });
    if(!removed.length && !added.length && !paramsChanged) return;
    // keep non-consumers + still-wanted consumers, refreshed from the load
    let kept = nodes.filter(n=>n.type!=="consumer" || wantIds.includes(n.id))
                    .map(n=> n.type==="consumer"
                      ? consumerNode(loads.find(l=>l.id===n.id)) : n);
    let fl = flows.filter(f=>!removed.includes(f.srcId)&&!removed.includes(f.tgtId));
    added.forEach(l=>{ const node=consumerNode(l); fl=[...fl,...newFlows(node,kept,fl)]; kept=[...kept,node]; });
    fl=compact(fl);
    const positioned=computePositions(kept);
    setNodes(positioned); setFlows(fl); notify(positioned,fl);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  },[cfg.loads]);

  /* sync when tech toggles change — add/remove nodes AND their flows together */
  useEffect(()=>{
    const types=new Set([
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
      if(t!=="consumer"&&!kept.some(n=>n.type===t)){
        const node=makeNode(t,kept);
        fl=[...fl, ...newFlows(node,kept,fl)];   // generate the new node's flows
        kept=[...kept,node];
        changed=true;
      }
    });
    if(changed){
      fl=compact(fl);
      const positioned=computePositions(kept);
      setNodes(positioned); setFlows(fl); notify(positioned,fl);
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  },[cfg.tech?.solar,cfg.tech?.wind,cfg.tech?.bess]);

  /* notify parent with full topology + derived signals + raw state for persistence */
  const notify=useCallback((n,fl)=>{
    if(!onTopoChange) return;
    const sig=deriveTopoSignals(n,fl);
    onTopoChange({
      nodes:n.map(nd=>({id:nd.id,type:nd.type,name:nd.name,...nd.params})),
      flows:[...fl].sort((a,b)=>a.pri-b.pri).map((f,i)=>({
        priority:i+1,
        source:n.find(x=>x.id===f.srcId)?.name,
        target:n.find(x=>x.id===f.tgtId)?.name,
        enabled:f.on, cap_mw:f.cap_mw
      })),
      signals: sig,
      // raw internal state (positions stripped) so the design rehydrates across nav
      _raw: { nodes:n.map(({_pos,...rest})=>rest), flows:fl },
    });
  },[onTopoChange]);

  // Emit initial signals + persist once on mount, so Step 4 has the topology even
  // if the user never touches the designer, and nav-state is seeded immediately.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(()=>{ notify(nodes,flows); },[]);

  /* actions */
  const addNode=useCallback(type=>{
    setNodes(prev=>{
      const node=makeNode(type,prev);
      const next=computePositions([...prev,node]);
      setFlows(fl=>{const nf=[...fl,...newFlows(node,prev,fl)];notify(next,nf);return nf;});
      return next;
    });
  },[notify]);

  const removeNode=useCallback(id=>{
    setNodes(prev=>{
      const n=prev.find(x=>x.id===id);
      if(!n) return prev;
      // guard: at least 1 consumer
      if(n.type==="consumer"&&prev.filter(x=>x.type==="consumer").length<=1) return prev;
      const next=computePositions(prev.filter(x=>x.id!==id));
      setFlows(fl=>{const nf=compact(fl.filter(f=>f.srcId!==id&&f.tgtId!==id));notify(next,nf);return nf;});
      return next;
    });
  },[notify]);

  const renameNode=useCallback((id,name)=>{
    if(!name?.trim()) return;
    setNodes(prev=>{const next=prev.map(n=>n.id===id?{...n,name:name.trim()}:n);notify(next,flows);return next;});
  },[flows,notify]);

  const setNodeParam=useCallback((id,key,val)=>{
    setNodes(prev=>{const next=prev.map(n=>n.id===id?{...n,params:{...n.params,[key]:val}}:n);notify(next,flows);return next;});
  },[flows,notify]);

  const toggleFlow=useCallback(id=>{
    setFlows(prev=>{const next=prev.map(f=>f.id===id?{...f,on:!f.on}:f);notify(nodes,next);return next;});
  },[nodes,notify]);

  const setFlowOn=useCallback((id,val)=>{
    setFlows(prev=>{const next=prev.map(f=>f.id===id?{...f,on:val}:f);notify(nodes,next);return next;});
  },[nodes,notify]);

  const setFlowCap=useCallback((id,val)=>{
    setFlows(prev=>prev.map(f=>f.id===id?{...f,cap_mw:val}:f));
  },[]);

  const reorder=useCallback((fromId,toId)=>{
    setFlows(prev=>{
      const fa=prev.find(f=>f.id===fromId),fb=prev.find(f=>f.id===toId);
      if(!fa||!fb) return prev;
      const next=compact(prev.map(f=>f.id===fromId?{...f,pri:fb.pri}:f.id===toId?{...f,pri:fa.pri}:f));
      notify(nodes,next); return next;
    });
  },[nodes,notify]);

  const reset=useCallback(()=>{
    _uid=0;
    const types=initTypes();
    const ns=buildNodes(types);
    const fl=buildFlows(ns);
    const wp=computePositions(ns);
    setNodes(wp); setFlows(fl); notify(wp,fl);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  },[cfg.tech]);

  const sorted=[...flows].sort((a,b)=>a.pri-b.pri);
  const sig=deriveTopoSignals(nodes,flows);
  const topoMeta=TOPO_META[sig.derived_topology]||TOPO_META.btm;

  return (
    <div className="card" style={{marginTop:20}}>
      <div style={{display:"flex",alignItems:"center",justifyContent:"space-between",marginBottom:2,flexWrap:"wrap",gap:8}}>
        <div style={{display:"flex",alignItems:"center",gap:10}}>
          <h3 style={{margin:0}}>⚡ Closed-Loop Energy Flow Design</h3>
          {/* Live topology badge */}
          <span style={{fontSize:"11px",fontWeight:700,padding:"3px 10px",borderRadius:20,
            background:topoMeta.bg,color:topoMeta.color,border:`1px solid ${topoMeta.color}40`,whiteSpace:"nowrap"}}>
            {topoMeta.label}
          </span>
        </div>
        <button onClick={reset} className="btn" style={{padding:"4px 10px",fontSize:"11.5px"}}>↺ Reset</button>
      </div>
      <div className="hint">Design the dispatch topology — which assets connect to which, and in what priority. Add/remove sources, storage and loads. Click a flow badge to toggle on/off; drag rows to reorder.</div>

      <div style={{display:"grid",gridTemplateColumns:"260px 1fr",gap:12,marginTop:12}}>
        <NodePanel nodes={nodes} onAdd={addNode} onRemove={removeNode} onRename={renameNode} onSetParam={setNodeParam}/>
        <div style={{border:"1px solid var(--line2)",borderRadius:8,background:"#fcfdfd",position:"relative",overflow:"hidden",height:340}}>
          <DiagramSVG nodes={nodes} flows={flows} onToggleFlow={toggleFlow}/>
          <div style={{position:"absolute",top:8,right:10,fontSize:"10px",color:"var(--grey)"}}>
            {sorted.length} flows · {sorted.filter(f=>f.on).length} active
          </div>
          <div style={{position:"absolute",bottom:7,left:"50%",transform:"translateX(-50%)",display:"flex",gap:14,fontSize:"10.5px",color:"var(--grey)",whiteSpace:"nowrap"}}>
            {[["Solar","#e0922f"],["Wind","#1f8a8a"],["BESS","#2f8f5b"],["Grid","#7a6f9b"]].map(([l,c])=>(
              <span key={l}><span style={{display:"inline-block",width:8,height:8,borderRadius:"50%",background:c,verticalAlign:"middle",marginRight:3}}/>{l}</span>
            ))}
          </div>
        </div>
      </div>

      {/* Derived signal summary strip */}
      <div style={{marginTop:10,padding:"8px 12px",background:"#f8fafa",border:"1px solid var(--line)",borderRadius:8,
        display:"flex",gap:12,flexWrap:"wrap",fontSize:"11px",color:"var(--grey)"}}>
        <span>Signals sent to resolver:</span>
        {[
          ["topology",sig.derived_topology],
          ["grid",sig.grid_available?"available":"absent"],
          ["off_grid",sig.off_grid?"yes":"no"],
          ["standalone",sig.standalone?"yes":"no"],
          sig.pv_mw!=null?["PV",`${sig.pv_mw} MW`]:null,
          sig.wind_mw!=null?["Wind",`${sig.wind_mw} MW`]:null,
          sig.has_bess?["BESS","present"]:null,
          ["peak_load",`${sig.peak_load_mw} MW`],
        ].filter(Boolean).map(([k,v])=>(
          <span key={k} style={{background:"#fff",border:"1px solid var(--line2)",borderRadius:8,padding:"2px 8px"}}>
            {k}: <b style={{color:"var(--slate)"}}>{String(v)}</b>
          </span>
        ))}
      </div>

      <div style={{marginTop:12}}>
        <div style={{fontSize:"11px",fontWeight:600,color:"var(--grey)",letterSpacing:".4px",marginBottom:2}}>
          ENERGY FLOWS <span style={{fontWeight:400}}>— toggle on/off to shape the topology</span>
        </div>
        <div className="subtle" style={{fontSize:"10.5px",marginBottom:6,fontStyle:"italic"}}>
          The engine enforces a fixed behind-the-meter dispatch order (direct → charge → discharge → grid), so these
          flows define the <b>topology</b> (what's connected), not a custom priority.
        </div>
        <FlowList nodes={nodes} flows={flows} onToggleFlow={toggleFlow}
          onSetFlowOn={setFlowOn} onSetFlowCap={setFlowCap} onReorder={reorder}/>
      </div>
    </div>
  );
}
