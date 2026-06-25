"""
Plotter Module for Phase 1 & Phase 2
------------------------------------
Generates interactive HTML plots for the sizing curves and co-optimisation surface using Plotly.
"""

from pathlib import Path
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from optimizer.schema import CurveCols


def plot_sizing_curves(
    output_dir: str | Path,
    curve_ssr: pd.DataFrame | None = None,
    curve_ps: pd.DataFrame | None = None,
    surface: pd.DataFrame | None = None,
) -> None:
    """
    Generate interactive HTML plots from the Phase 1/2 dataframes and save them.
    """
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    
    print(f"\n📈 Generating interactive plots in {out_dir.resolve()} ...")
    
    # 1. SSR Curve Plot
    if curve_ssr is not None and not curve_ssr.empty:
        df = curve_ssr[curve_ssr.get(CurveCols.FEASIBLE, True) == True].copy()
        if not df.empty and CurveCols.ACHIEVED_SSR_PCT in df.columns:
            df = df.sort_values(CurveCols.ACHIEVED_SSR_PCT)
            
            custom_data = df[[CurveCols.BESS_MWH, CurveCols.BESS_MW, CurveCols.PEAK_GC_MW]].values
            
            fig = make_subplots(specs=[[{"secondary_y": True}]])
            
            fig.add_trace(
                go.Scatter(x=df[CurveCols.ACHIEVED_SSR_PCT], y=df[CurveCols.BESS_MWH],
                           customdata=custom_data,
                           name="BESS Energy (MWh)", mode='lines+markers',
                           line=dict(color='blue', width=2), marker=dict(size=8),
                           hovertemplate="SSR: %{x:.1f}%<br>BESS Energy: %{customdata[0]:.1f} MWh<br>BESS Power: %{customdata[1]:.1f} MW<br>Grid Connection: %{customdata[2]:.1f} MW<extra></extra>"),
                secondary_y=False,
            )
            
            fig.add_trace(
                go.Scatter(x=df[CurveCols.ACHIEVED_SSR_PCT], y=df[CurveCols.BESS_MW],
                           customdata=custom_data,
                           name="BESS Power (MW)", mode='lines+markers',
                           line=dict(color='red', width=2, dash='dash'), marker=dict(size=8, symbol='square'),
                           hovertemplate="SSR: %{x:.1f}%<br>BESS Energy: %{customdata[0]:.1f} MWh<br>BESS Power: %{customdata[1]:.1f} MW<br>Grid Connection: %{customdata[2]:.1f} MW<extra></extra>"),
                secondary_y=True,
            )
            
            fig.update_layout(
                title_text="<b>Self-Sufficiency Sizing Curve</b>",
                xaxis_title="Achieved SSR (%)",
                hovermode="x unified",
                template="plotly_white",
                legend=dict(yanchor="top", y=0.99, xanchor="left", x=0.01)
            )
            fig.update_yaxes(title_text="<b>BESS Energy (MWh)</b>", color="blue", secondary_y=False)
            fig.update_yaxes(title_text="<b>BESS Power (MW)</b>", color="red", secondary_y=True)
            
            out_file = out_dir / "plot_ssr_curve.html"
            fig.write_html(str(out_file))
            print(f"   ✓ {out_file.name}")

    # 2. Peak Shaving Curve Plot
    if curve_ps is not None and not curve_ps.empty:
        df = curve_ps[curve_ps.get(CurveCols.FEASIBLE, True) == True].copy()
        if not df.empty and CurveCols.PEAK_GC_MW in df.columns:
            df = df.sort_values(CurveCols.PEAK_GC_MW, ascending=False)
            
            custom_data = df[[CurveCols.BESS_MWH, CurveCols.BESS_MW, CurveCols.PEAK_GC_MW]].values
            
            fig = make_subplots(specs=[[{"secondary_y": True}]])
            
            fig.add_trace(
                go.Scatter(x=df[CurveCols.PEAK_GC_MW], y=df[CurveCols.BESS_MWH],
                           customdata=custom_data,
                           name="BESS Energy (MWh)", mode='lines+markers',
                           line=dict(color='blue', width=2), marker=dict(size=8),
                           hovertemplate="Grid Limit: %{customdata[2]:.1f} MW<br>BESS Energy: %{customdata[0]:.1f} MWh<br>BESS Power: %{customdata[1]:.1f} MW<extra></extra>"),
                secondary_y=False,
            )
            
            fig.add_trace(
                go.Scatter(x=df[CurveCols.PEAK_GC_MW], y=df[CurveCols.BESS_MW],
                           customdata=custom_data,
                           name="BESS Power (MW)", mode='lines+markers',
                           line=dict(color='red', width=2, dash='dash'), marker=dict(size=8, symbol='square'),
                           hovertemplate="Grid Limit: %{customdata[2]:.1f} MW<br>BESS Energy: %{customdata[0]:.1f} MWh<br>BESS Power: %{customdata[1]:.1f} MW<extra></extra>"),
                secondary_y=True,
            )
            
            fig.update_layout(
                title_text="<b>Peak Shaving Sizing Curve</b>",
                xaxis_title="Target Grid Limit (MW)",
                xaxis=dict(autorange="reversed"),  # Tighter grid limits to the right
                hovermode="x unified",
                template="plotly_white",
                legend=dict(yanchor="top", y=0.99, xanchor="right", x=0.99)
            )
            fig.update_yaxes(title_text="<b>BESS Energy (MWh)</b>", color="blue", secondary_y=False)
            fig.update_yaxes(title_text="<b>BESS Power (MW)</b>", color="red", secondary_y=True)
            
            out_file = out_dir / "plot_peakshaving_curve.html"
            fig.write_html(str(out_file))
            print(f"   ✓ {out_file.name}")

    # 3. PV+BESS Co-Sizing Surface Plot
    if surface is not None and not surface.empty:
        df = surface[surface.get(CurveCols.FEASIBLE, True) == True].copy()
        if not df.empty and CurveCols.PV_MW in df.columns:
            
            x = df[CurveCols.PV_MW]
            y = df[CurveCols.TARGET_SSR_PCT] if CurveCols.TARGET_SSR_PCT in df.columns else df[CurveCols.ACHIEVED_SSR_PCT]
            z = df[CurveCols.BESS_MWH]
            
            # Combine custom data: BESS MW and Peak GC MW
            custom_data = df[[CurveCols.BESS_MW, CurveCols.PEAK_GC_MW]].values
            
            fig = go.Figure(data=[go.Mesh3d(
                x=x, y=y, z=z,
                customdata=custom_data,
                opacity=0.8,
                colorscale='Viridis',
                intensity=z,
                hovertemplate="PV: %{x} MW<br>SSR: %{y}%<br>BESS Energy: %{z:.1f} MWh<br>BESS Power: %{customdata[0]:.1f} MW<br>Grid Connection: %{customdata[1]:.1f} MW<extra></extra>"
            )])
            
            # Add the actual points
            fig.add_trace(go.Scatter3d(
                x=x, y=y, z=z,
                customdata=custom_data,
                mode='markers',
                marker=dict(size=5, color='black'),
                name='Sizing Points',
                hovertemplate="PV: %{x} MW<br>SSR: %{y}%<br>BESS Energy: %{z:.1f} MWh<br>BESS Power: %{customdata[0]:.1f} MW<br>Grid Connection: %{customdata[1]:.1f} MW<extra></extra>"
            ))
            
            fig.update_layout(
                title_text="<b>PV + BESS Co-Sizing Surface</b>",
                scene=dict(
                    xaxis_title='PV Nameplate (MW)',
                    yaxis_title='Target SSR (%)',
                    zaxis_title='BESS Energy (MWh)'
                ),
                margin=dict(l=0, r=0, b=0, t=50)
            )
            
            out_file = out_dir / "plot_surface.html"
            fig.write_html(str(out_file))
            print(f"   ✓ {out_file.name}")

