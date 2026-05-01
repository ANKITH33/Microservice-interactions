"""
Microservice Observability Dashboard
Run: streamlit run dashboard.py
"""

import json, os
import streamlit as st
import plotly.graph_objects as go
import plotly.express as px
import pandas as pd
import networkx as nx

# ─── Page config ────────────────────────────────────────────────────────────
st.set_page_config(page_title="Microservice Dashboard", layout="wide", page_icon="🔬")
st.markdown("""
<style>
  .block-container { padding-top: 1.5rem; }
  h1 { font-size: 1.6rem; }
  h2 { font-size: 1.2rem; border-bottom: 1px solid #333; padding-bottom: 4px; }
</style>
""", unsafe_allow_html=True)

# ─── Data loading ────────────────────────────────────────────────────────────
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outputs")

def load(filename):
    candidates = [
        os.path.join(DATA_DIR, filename),
        os.path.join("/mnt/user-data/uploads", filename),
    ]
    for path in candidates:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    return None

@st.cache_data(ttl=300)
def load_all():
    return (
        load("bottlenecks.json") or {},
        load("graph-service.json") or {},
        load("graph-endpoint.json") or {},
        load("metrics-service.json") or [],
        load("metrics-endpoint.json") or [],
        load("prometheus-processed.json") or {},
    )

@st.cache_data
def compute_graph_layout(nodes_key, edges_key):
    """Cache the expensive spring_layout computation."""
    import json as _json
    nodes = _json.loads(nodes_key)
    edges = _json.loads(edges_key)
    G = nx.DiGraph()
    for n in nodes:
        G.add_node(short(n["service"]))
    for e in edges:
        G.add_edge(short(e["caller"]), short(e["callee"]),
                   weight=e["call_count"], p99=e["latency"]["p99_ms"])
    pos = nx.spring_layout(G, seed=42, k=2.5)
    # Convert to serializable form
    pos_serializable = {k: list(v) for k, v in pos.items()}
    in_degrees = {n: G.in_degree(n) for n in G.nodes()}
    return pos_serializable, in_degrees, list(G.nodes()), list(G.edges())

bottlenecks, graph_service, graph_endpoint, metrics_svc, metrics_ep, prometheus = load_all()

def short(name):
    return name.replace(".default", "")

# ─── Sidebar ─────────────────────────────────────────────────────────────────
st.sidebar.title("🔬 Observability")
page = st.sidebar.radio("View", [
    "📊 Bottleneck Overview",
    "⏱ Latency Analysis",
    "🔗 Dependency Graph",
    "📐 Cohesion & Coupling",
    "⚡ Service Instability",
    "🚦 Traffic & Errors",
    "💻 Resource Usage",
    "🗂 Data Tables",
])

# ═══════════════════════════════════════════════════════════════════════════════
# 1. BOTTLENECK OVERVIEW
# ═══════════════════════════════════════════════════════════════════════════════
if page == "📊 Bottleneck Overview":
    st.title("📊 Bottleneck Overview")

    all_scores = bottlenecks.get("all_service_scores", [])
    if not all_scores:
        st.warning("No bottleneck data found."); st.stop()

    df = pd.DataFrame(all_scores)
    df["service"] = df["service"].apply(short)
    df = df.sort_values("bottleneck_score", ascending=False)
    color_map = {"high": "#ef4444", "medium": "#f97316", "low": "#22c55e"}
    df["color"] = df["severity"].map(color_map).fillna("#6b7280")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Services", len(df))
    c2.metric("🔴 High",   int((df["severity"]=="high").sum()))
    c3.metric("🟠 Medium", int((df["severity"]=="medium").sum()))
    c4.metric("🟢 Low",    int((df["severity"]=="low").sum()))
    st.markdown("---")

    fig = go.Figure(go.Bar(
        x=df["service"], y=df["bottleneck_score"],
        marker_color=df["color"],
        text=df["bottleneck_score"].round(2), textposition="outside",
        hovertemplate="<b>%{x}</b><br>Score: %{y:.2f}<extra></extra>",
    ))
    fig.update_layout(title="Bottleneck Score by Service", xaxis_tickangle=-30,
        yaxis_title="Score", plot_bgcolor="#0e1117", paper_bgcolor="#0e1117",
        font_color="white", height=400)
    st.plotly_chart(fig, use_container_width=True)

    fig2 = go.Figure(go.Bar(
        x=df["service"], y=df["risk_score"],
        marker_color="#818cf8",
        text=df["risk_score"].round(3), textposition="outside",
    ))
    fig2.update_layout(title="Risk Score by Service", xaxis_tickangle=-30,
        yaxis_title="Risk Score", plot_bgcolor="#0e1117", paper_bgcolor="#0e1117",
        font_color="white", height=380)
    st.plotly_chart(fig2, use_container_width=True)

    cp = bottlenecks.get("critical_path", {})
    if cp:
        st.subheader("🛑 Critical Path")
        path_svcs = cp.get("path", [])
        lats      = cp.get("latencies_ms", [])
        st.markdown(f"**Path:** {' → '.join([short(s) for s in path_svcs])}")
        st.markdown(f"**Total P99:** `{cp.get('total_p99_ms',0):.1f} ms`")
        fig3 = go.Figure(go.Bar(
            x=[short(s) for s in path_svcs], y=lats,
            marker_color="#f43f5e",
            text=[f"{v:.1f}" for v in lats], textposition="outside",
        ))
        fig3.update_layout(title="Critical Path P99 per Hop", plot_bgcolor="#0e1117",
            paper_bgcolor="#0e1117", font_color="white", height=320)
        st.plotly_chart(fig3, use_container_width=True)

# ═══════════════════════════════════════════════════════════════════════════════
# 2. LATENCY ANALYSIS
# ═══════════════════════════════════════════════════════════════════════════════
elif page == "⏱ Latency Analysis":
    st.title("⏱ Latency Analysis")
    if not metrics_svc:
        st.warning("No metrics-service.json found."); st.stop()

    df = pd.DataFrame([{
        "service": short(s["service"]),
        "p50": s["latency"]["p50_ms"], "p95": s["latency"]["p95_ms"],
        "p99": s["latency"]["p99_ms"], "mean": s["latency"]["mean_ms"],
        "tail_ratio": s.get("tail_ratio", 0),
    } for s in metrics_svc]).sort_values("p99", ascending=False)

    fig = go.Figure()
    for m, c in [("p50","#22d3ee"),("p95","#f97316"),("p99","#ef4444"),("mean","#a78bfa")]:
        fig.add_trace(go.Bar(name=m.upper(), x=df["service"], y=df[m], marker_color=c))
    fig.update_layout(barmode="group", title="Latency Percentiles",
        xaxis_tickangle=-30, yaxis_title="ms",
        plot_bgcolor="#0e1117", paper_bgcolor="#0e1117", font_color="white", height=420)
    st.plotly_chart(fig, use_container_width=True)

    fig2 = go.Figure(go.Bar(
        x=df["service"], y=df["tail_ratio"],
        marker_color=[("#ef4444" if v>=5 else "#f97316" if v>=3 else "#22c55e") for v in df["tail_ratio"]],
        text=df["tail_ratio"].round(2), textposition="outside",
    ))
    fig2.add_hline(y=2, line_dash="dot", line_color="white", annotation_text="threshold=2×")
    fig2.update_layout(title="Tail Ratio (P99/P50)", xaxis_tickangle=-30, yaxis_title="P99/P50",
        plot_bgcolor="#0e1117", paper_bgcolor="#0e1117", font_color="white", height=360)
    st.plotly_chart(fig2, use_container_width=True)

    heat_df = df.set_index("service")[["p50","p95","p99","mean"]]
    fig3 = px.imshow(heat_df.T, text_auto=".1f", color_continuous_scale="YlOrRd",
                     title="Latency Heatmap (ms)")
    fig3.update_layout(plot_bgcolor="#0e1117", paper_bgcolor="#0e1117",
                       font_color="white", height=280)
    st.plotly_chart(fig3, use_container_width=True)

    if metrics_ep:
        st.subheader("Top Endpoints by P99")
        ep_df = pd.DataFrame([{
            "endpoint": short(e["service"]) + "/" + e["operation"].split("/")[-1],
            "p50": e["latency"]["p50_ms"], "p99": e["latency"]["p99_ms"],
        } for e in metrics_ep]).sort_values("p99", ascending=False).head(10)
        fig4 = go.Figure()
        fig4.add_trace(go.Bar(name="P50", x=ep_df["endpoint"], y=ep_df["p50"], marker_color="#22d3ee"))
        fig4.add_trace(go.Bar(name="P99", x=ep_df["endpoint"], y=ep_df["p99"], marker_color="#ef4444"))
        fig4.update_layout(barmode="group", xaxis_tickangle=-30, yaxis_title="ms",
            plot_bgcolor="#0e1117", paper_bgcolor="#0e1117", font_color="white", height=380)
        st.plotly_chart(fig4, use_container_width=True)

# ═══════════════════════════════════════════════════════════════════════════════
# 3. DEPENDENCY GRAPH
# ═══════════════════════════════════════════════════════════════════════════════
elif page == "🔗 Dependency Graph":
    st.title("🔗 Service Dependency Graph")

    edges = graph_service.get("edges", [])
    nodes = graph_service.get("nodes", [])
    if not edges:
        st.warning("No graph-service.json found."); st.stop()

    import json as _json
    pos_map, in_degrees, g_nodes, g_edges = compute_graph_layout(
        _json.dumps(nodes, sort_keys=True),
        _json.dumps(edges, sort_keys=True),
    )

    edge_x, edge_y = [], []
    for u, v in g_edges:
        x0,y0 = pos_map[u]; x1,y1 = pos_map[v]
        edge_x += [x0,x1,None]; edge_y += [y0,y1,None]

    edge_trace = go.Scatter(x=edge_x, y=edge_y, mode="lines",
        line=dict(width=1.2, color="#475569"), hoverinfo="none")

    sev_map   = {short(s["service"]): s.get("severity","low") for s in bottlenecks.get("all_service_scores",[])}
    sev_color = {"high":"#ef4444","medium":"#f97316","low":"#22c55e"}
    node_x     = [pos_map[n][0] for n in g_nodes]
    node_y     = [pos_map[n][1] for n in g_nodes]
    node_colors= [sev_color.get(sev_map.get(n,"low"),"#6b7280") for n in g_nodes]
    node_sizes = [20 + in_degrees.get(n,0)*8 for n in g_nodes]

    node_trace = go.Scatter(x=node_x, y=node_y, mode="markers+text",
        marker=dict(size=node_sizes, color=node_colors, line=dict(width=1.5,color="white")),
        text=g_nodes, textposition="top center",
        hovertemplate="<b>%{text}</b><extra></extra>")

    fig = go.Figure([edge_trace, node_trace])
    fig.update_layout(showlegend=False, hovermode="closest",
        xaxis=dict(showgrid=False,zeroline=False,showticklabels=False),
        yaxis=dict(showgrid=False,zeroline=False,showticklabels=False),
        plot_bgcolor="#0e1117", paper_bgcolor="#0e1117", font_color="white", height=560,
        title="Service Call Graph  (node size = fan-in, color = severity: 🔴high 🟠medium 🟢low)")
    st.plotly_chart(fig, use_container_width=True)

    st.subheader("Call Edges")
    st.dataframe(pd.DataFrame([{
        "Caller": short(e["caller"]), "Callee": short(e["callee"]),
        "Calls": e["call_count"], "P99 ms": e["latency"]["p99_ms"],
        "Errors": e["error_count"],
    } for e in edges]).sort_values("Calls", ascending=False), use_container_width=True)

# ═══════════════════════════════════════════════════════════════════════════════
# 4. COHESION & COUPLING
# ═══════════════════════════════════════════════════════════════════════════════
elif page == "📐 Cohesion & Coupling":
    st.title("📐 Service Cohesion & Coupling")
    if not metrics_svc:
        st.warning("No metrics-service.json found."); st.stop()

    df = pd.DataFrame([{
        "service": short(s["service"]),
        "SIDC": s["cohesion"]["sidc"], "SIUC": s["cohesion"]["siuc"], "TSIC": s["cohesion"]["tsic"],
        "AIS": s["coupling"]["ais"], "ADS": s["coupling"]["ads"],
        "ACS": s["coupling"]["acs"], "SDP": s["coupling"]["sdp"],
        "endpoints": s["cohesion"]["num_endpoints"],
    } for s in metrics_svc if "cohesion" in s])

    col1, col2 = st.columns(2)
    with col1:
        fig = go.Figure()
        for m,c in [("SIDC","#3b82f6"),("SIUC","#22c55e"),("TSIC","#f97316")]:
            fig.add_trace(go.Bar(name=m, x=df["service"], y=df[m], marker_color=c))
        fig.update_layout(barmode="group", title="Cohesion (SIDC / SIUC / TSIC)",
            xaxis_tickangle=-30, yaxis_title="Score",
            plot_bgcolor="#0e1117", paper_bgcolor="#0e1117", font_color="white", height=400)
        st.plotly_chart(fig, use_container_width=True)

    with col2:
        fig2 = go.Figure()
        for m,c in [("AIS","#ec4899"),("ADS","#8b5cf6"),("ACS","#7c3aed")]:
            fig2.add_trace(go.Bar(name=m, x=df["service"], y=df[m], marker_color=c))
        fig2.update_layout(barmode="group", title="Coupling (AIS / ADS / ACS)",
            xaxis_tickangle=-30, yaxis_title="Count",
            plot_bgcolor="#0e1117", paper_bgcolor="#0e1117", font_color="white", height=400)
        st.plotly_chart(fig2, use_container_width=True)

    fig3 = go.Figure(go.Bar(
        x=df["service"], y=df["SDP"],
        marker_color=[("#ef4444" if v>=0.8 else "#f97316" if v>=0.4 else "#22c55e") for v in df["SDP"]],
        text=df["SDP"].round(2), textposition="outside",
    ))
    fig3.add_hline(y=0.5, line_dash="dot", line_color="white", annotation_text="SDP=0.5")
    fig3.update_layout(title="Instability (SDP = ADS / (AIS+ADS))",
        xaxis_tickangle=-30, yaxis_title="SDP",
        plot_bgcolor="#0e1117", paper_bgcolor="#0e1117", font_color="white", height=360)
    st.plotly_chart(fig3, use_container_width=True)

    fig4 = px.scatter(df, x="AIS", y="TSIC", text="service", color="TSIC",
                      color_continuous_scale="RdYlGn", size="endpoints",
                      title="Cohesion (TSIC) vs Fan-In (AIS)")
    fig4.update_traces(textposition="top center")
    fig4.update_layout(plot_bgcolor="#0e1117", paper_bgcolor="#0e1117",
                       font_color="white", height=400)
    st.plotly_chart(fig4, use_container_width=True)

# ═══════════════════════════════════════════════════════════════════════════════
# 5. SERVICE INSTABILITY (KMamiz-style)
# ═══════════════════════════════════════════════════════════════════════════════
elif page == "⚡ Service Instability":
    st.title("⚡ Service Instability — FanIn / FanOut / SDP")
    if not metrics_svc:
        st.warning("No metrics-service.json found."); st.stop()

    df = pd.DataFrame([{
        "service": short(s["service"]),
        "FanIn":  s["coupling"]["ais"],
        "FanOut": s["coupling"]["ads"],
        "SDP":    s["coupling"]["sdp"],
    } for s in metrics_svc]).sort_values("FanIn", ascending=False)

    fig = go.Figure()
    fig.add_trace(go.Bar(name="FanOut (ADS)", x=df["service"], y=df["FanOut"],
                         marker_color="#22d3ee"))
    fig.add_trace(go.Bar(name="FanIn (AIS)",  x=df["service"], y=df["FanIn"],
                         marker_color="#ec4899"))
    fig.add_trace(go.Scatter(name="SDP", x=df["service"], y=df["SDP"],
        mode="markers+text",
        marker=dict(color="#f59e0b", size=14, symbol="diamond"),
        text=["SDP: "+str(round(v,2)) for v in df["SDP"]],
        textposition="top center", yaxis="y2"))
    fig.update_layout(
        barmode="group", title="Service Instability",
        xaxis_tickangle=-30,
        yaxis=dict(title="FanIn / FanOut"),
        yaxis2=dict(title="SDP", overlaying="y", side="right", range=[0,1.3], color="#f59e0b"),
        plot_bgcolor="#0e1117", paper_bgcolor="#0e1117",
        font_color="white", height=460, legend=dict(orientation="h"),
    )
    st.plotly_chart(fig, use_container_width=True)

# ═══════════════════════════════════════════════════════════════════════════════
# 6. TRAFFIC & ERRORS
# ═══════════════════════════════════════════════════════════════════════════════
elif page == "🚦 Traffic & Errors":
    st.title("🚦 Traffic & Errors")

    prom_svcs = prometheus.get("services", [])
    df_prom = pd.DataFrame([s for s in prom_svcs if s["request_rate"] > 0])
    if not df_prom.empty:
        df_prom = df_prom.sort_values("request_rate", ascending=False)
        fig = go.Figure(go.Bar(x=df_prom["service"], y=df_prom["request_rate"],
            marker_color="#38bdf8",
            text=df_prom["request_rate"].round(2), textposition="outside"))
        fig.update_layout(title="Request Rate (req/s)", xaxis_tickangle=-30,
            yaxis_title="req/s", plot_bgcolor="#0e1117", paper_bgcolor="#0e1117",
            font_color="white", height=380)
        st.plotly_chart(fig, use_container_width=True)

    edges = graph_service.get("edges", [])
    if edges:
        edge_df = pd.DataFrame([{
            "Edge": f"{short(e['caller'])} → {short(e['callee'])}",
            "Calls": e["call_count"], "P99 ms": e["latency"]["p99_ms"],
        } for e in edges]).sort_values("Calls", ascending=True)
        fig2 = go.Figure(go.Bar(
            x=edge_df["Calls"], y=edge_df["Edge"], orientation="h",
            marker_color="#818cf8",
            text=edge_df["Calls"], textposition="outside"))
        fig2.update_layout(title="Call Count per Edge",
            plot_bgcolor="#0e1117", paper_bgcolor="#0e1117",
            font_color="white", height=max(300, len(edge_df)*34))
        st.plotly_chart(fig2, use_container_width=True)

    if metrics_ep:
        ep_df = pd.DataFrame([{
            "endpoint": short(e["service"]) + "/" + e["operation"].split("/")[-1],
            "req/s": e.get("request_rate", 0),
        } for e in metrics_ep]).sort_values("req/s", ascending=False).head(15)
        fig3 = px.bar(ep_df, x="req/s", y="endpoint", orientation="h",
                      color="req/s", color_continuous_scale="Blues",
                      title="Top Endpoints by Request Rate")
        fig3.update_layout(plot_bgcolor="#0e1117", paper_bgcolor="#0e1117",
                           font_color="white", height=440)
        st.plotly_chart(fig3, use_container_width=True)

# ═══════════════════════════════════════════════════════════════════════════════
# 7. RESOURCE USAGE
# ═══════════════════════════════════════════════════════════════════════════════
elif page == "💻 Resource Usage":
    st.title("💻 Resource Usage (Prometheus)")
    prom_svcs = prometheus.get("services", [])
    df = pd.DataFrame([s for s in prom_svcs if s["cpu_cores"] > 0 or s["memory_mb"] > 0])
    if df.empty:
        st.info("No Prometheus resource data available.")
    else:
        col1, col2 = st.columns(2)
        with col1:
            fig = go.Figure(go.Bar(x=df["service"], y=df["cpu_cores"],
                marker_color="#4ade80",
                text=df["cpu_cores"].round(4), textposition="outside"))
            fig.update_layout(title="CPU (cores)", xaxis_tickangle=-30,
                plot_bgcolor="#0e1117", paper_bgcolor="#0e1117", font_color="white", height=360)
            st.plotly_chart(fig, use_container_width=True)
        with col2:
            fig2 = go.Figure(go.Bar(x=df["service"], y=df["memory_mb"],
                marker_color="#fb923c",
                text=df["memory_mb"].round(1), textposition="outside"))
            fig2.update_layout(title="Memory (MB)", xaxis_tickangle=-30,
                plot_bgcolor="#0e1117", paper_bgcolor="#0e1117", font_color="white", height=360)
            st.plotly_chart(fig2, use_container_width=True)

        df2 = df[df["request_rate"] > 0].copy()
        if not df2.empty:
            fig3 = px.scatter(df2, x="cpu_cores", y="memory_mb",
                              size="request_rate", text="service",
                              color="request_rate", color_continuous_scale="Viridis",
                              title="CPU vs Memory (bubble = request rate)")
            fig3.update_traces(textposition="top center")
            fig3.update_layout(plot_bgcolor="#0e1117", paper_bgcolor="#0e1117",
                               font_color="white", height=420)
            st.plotly_chart(fig3, use_container_width=True)

# ═══════════════════════════════════════════════════════════════════════════════
# 8. DATA TABLES
# ═══════════════════════════════════════════════════════════════════════════════
elif page == "🗂 Data Tables":
    st.title("🗂 Data Tables")
    tab1, tab2, tab3, tab4 = st.tabs([
        "Service Bottlenecks", "Endpoint Bottlenecks", "Service Metrics", "Endpoint Metrics"
    ])

    with tab1:
        rows = bottlenecks.get("service_bottlenecks", [])
        if rows:
            st.dataframe(pd.DataFrame([{
                "Service": short(r["service"]), "Severity": r["severity"],
                "Score": round(r["bottleneck_score"],3), "Risk": round(r["risk_score"],3),
                "P50 ms": r["latency"]["p50_ms"], "P99 ms": r["latency"]["p99_ms"],
                "Tail": round(r.get("tail_ratio",0),2),
                "AIS": r["coupling"]["ais"], "TSIC": round(r["cohesion"]["tsic"],3),
                "Reasons": " | ".join(r.get("reasons",[])),
            } for r in rows]), use_container_width=True)

    with tab2:
        rows = bottlenecks.get("endpoint_bottlenecks", [])
        if rows:
            st.dataframe(pd.DataFrame([{
                "Service": short(r["service"]),
                "Operation": r["operation"].split("/")[-1],
                "Severity": r["severity"],
                "Score": round(r["bottleneck_score"],3), "Risk": round(r["risk_score"],3),
                "P50 ms": r["latency"]["p50_ms"], "P99 ms": r["latency"]["p99_ms"],
                "Calls": r.get("call_count",0), "Req/s": round(r.get("request_rate",0),3),
                "Reasons": " | ".join(r.get("reasons",[])),
            } for r in rows]), use_container_width=True)

    with tab3:
        if metrics_svc:
            st.dataframe(pd.DataFrame([{
                "Service": short(s["service"]), "Calls": s["call_count"],
                "P50 ms": s["latency"]["p50_ms"], "P99 ms": s["latency"]["p99_ms"],
                "Tail": round(s.get("tail_ratio",0),2),
                "AIS": s["coupling"]["ais"], "ADS": s["coupling"]["ads"],
                "SDP": round(s["coupling"]["sdp"],3), "TSIC": round(s["cohesion"]["tsic"],3),
                "Score": round(s.get("bottleneck_score",0),3),
            } for s in metrics_svc]), use_container_width=True)

    with tab4:
        if metrics_ep:
            st.dataframe(pd.DataFrame([{
                "Service": short(e["service"]),
                "Operation": e["operation"].split("/")[-1],
                "Calls": e.get("call_count",0), "Req/s": round(e.get("request_rate",0),3),
                "P50 ms": e["latency"]["p50_ms"], "P99 ms": e["latency"]["p99_ms"],
                "Tail": round(e.get("tail_ratio",0),2),
                "Score": round(e.get("bottleneck_score",0),3),
            } for e in metrics_ep]).sort_values("Score", ascending=False),
            use_container_width=True)