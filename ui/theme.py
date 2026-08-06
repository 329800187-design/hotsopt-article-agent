from __future__ import annotations

from pathlib import Path


COLORS = {
    "ink": "#101820",
    "ink_soft": "#24313a",
    "ivory": "#f5f2eb",
    "paper": "#fffdf8",
    "copper": "#b99055",
    "copper_dark": "#8f6c3e",
    "line": "#ded6c8",
    "muted": "#6e746f",
    "success": "#39715a",
    "warning": "#a66b2d",
    "danger": "#a34d43",
}


def stylesheet() -> str:
    c = COLORS
    return f"""
    <style>
    :root {{ --ink:{c['ink']}; --ivory:{c['ivory']}; --paper:{c['paper']}; --copper:{c['copper']}; --line:{c['line']}; }}
    .stApp {{ background:var(--ivory); color:var(--ink); }}
    [data-testid="stSidebar"] {{ background:var(--ink); border-right:4px solid var(--copper); }}
    [data-testid="stSidebar"] * {{ color:var(--ivory); }}
    [data-testid="stSidebar"] [role="radiogroup"] label {{ padding:.38rem .5rem; border-radius:6px; font-weight:650; }}
    [data-testid="stSidebar"] [role="radiogroup"] label:hover {{ background:rgba(255,255,255,.09); }}
    .block-container {{ max-width:1440px; padding-top:2.3rem; padding-bottom:4rem; }}
    h1,h2,h3 {{ letter-spacing:-.02em; color:var(--ink); }}
    [data-testid="stMetric"] {{ background:var(--paper); border:1px solid var(--line); border-radius:8px; box-shadow:0 4px 16px rgba(16,24,32,.05); }}
    div.stButton > button {{ border-radius:6px; border-color:var(--line); min-height:2.5rem; }}
    div.stButton > button[kind="primary"] {{ background:var(--copper); border-color:var(--copper); color:#fff; }}
    div.stButton > button[kind="primary"]:hover {{ background:var(--copper_dark); border-color:var(--copper_dark); }}
    [data-testid="stExpander"] {{ background:var(--paper); border:1px solid var(--line); border-radius:8px; }}
    .rc1-kicker {{ color:var(--copper_dark); font-size:.78rem; letter-spacing:.18em; text-transform:uppercase; font-weight:700; }}
    .rc1-stage {{ color:var(--muted); font-size:.9rem; }}
    .rc1-rule {{ border-top:1px solid var(--line); margin:1.5rem 0; }}
    .rc1-hero {{ background:linear-gradient(105deg,#101820 0%,#26343a 72%,#8f6c3e 150%); color:#fffdf8; border-radius:12px; padding:1.35rem 1.55rem; margin-bottom:1.15rem; box-shadow:0 10px 28px rgba(16,24,32,.14); }}
    .rc1-hero h2 {{ color:#fffdf8; margin:0 0 .35rem; font-size:1.65rem; }}
    .rc1-hero p {{ color:#f5f2eb; margin:0; max-width:48rem; }}
    .rc1-card {{ background:var(--paper); border:1px solid var(--line); border-radius:9px; padding:.75rem .9rem; margin-bottom:.65rem; }}
    .rc1-card-title {{ color:var(--ink); font-weight:700; line-height:1.4; }}
    .rc1-card:hover {{ border-color:var(--copper); box-shadow:0 2px 12px rgba(185,144,85,.12); }}
    [data-testid="stTabs"] {{ margin-top:0.5rem; }}
    [data-testid="stTabs"] button {{ font-weight:650; font-size:.95rem; }}
    .rc1-cost-badge {{ display:inline-block; padding:.15rem .55rem; border-radius:4px; font-size:.78rem; font-weight:650; }}
    .rc1-cost-badge.low {{ background:#e8f5e9; color:#2e7d32; }}
    .rc1-cost-badge.mid {{ background:#fff8e1; color:#f57f17; }}
    .rc1-cost-badge.std {{ background:#e3f2fd; color:#1565c0; }}
    [data-testid="stToolbar"] {{ visibility:hidden; height:0; }}
    [data-testid="stDecoration"] {{ display:none; }}
    footer {{ visibility:hidden; height:0; }}
    </style>
    """


def apply() -> None:
    import streamlit as st

    st.markdown(stylesheet(), unsafe_allow_html=True)
