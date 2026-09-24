"""Navy/teal visual theme + small reusable render helpers (Phase 8 §4).

Premium educational SaaS direction: light background, navy/teal accents,
Inter typeface, spacious cards, minimal animation. Injected once per page
load via ``inject_theme()``.
"""
from __future__ import annotations

from typing import Optional

import streamlit as st

NAVY = "#0B2545"
NAVY_LIGHT = "#13315C"
TEAL = "#0FA3A3"
TEAL_LIGHT = "#E6F7F6"
INK = "#1A2333"
MUTED = "#5B6B82"
BORDER = "#E3E8EF"
BG = "#F7F9FC"
SURFACE = "#FFFFFF"

_STATUS_COLORS = {
    "PLANNING": ("#EEF2FF", "#3B4CCA"),
    "RUNNING": ("#E6F7F6", "#0FA3A3"),
    "VERIFYING": ("#FFF7E6", "#B98900"),
    "WAITING_FOR_APPROVAL": ("#FFF1E0", "#C2660D"),
    "COMPLETED": ("#E7F8EE", "#1E8E5A"),
    "NEEDS_REVIEW": ("#FFF1E0", "#C2660D"),
    "FAILED": ("#FDEAEA", "#C23B3B"),
    # Mission/task/approval status values (lowercase, from the backend enums)
    "completed": ("#E7F8EE", "#1E8E5A"),
    "needs_approval": ("#FFF1E0", "#C2660D"),
    "needs_replan": ("#FFF1E0", "#C2660D"),
    "failed": ("#FDEAEA", "#C23B3B"),
    "in_progress": ("#E6F7F6", "#0FA3A3"),
    "pending": ("#EEF2FF", "#3B4CCA"),
    "blocked": ("#FFF1E0", "#C2660D"),
    "skipped": ("#F1F3F7", "#5B6B82"),
    "success": ("#E7F8EE", "#1E8E5A"),
    "partial": ("#FFF1E0", "#C2660D"),
    "verified": ("#E7F8EE", "#1E8E5A"),
    "needs_review": ("#FFF1E0", "#C2660D"),
    "approved": ("#E7F8EE", "#1E8E5A"),
    "rejected": ("#FDEAEA", "#C23B3B"),
    "stale": ("#F1F3F7", "#5B6B82"),
    "edit_required": ("#FFF1E0", "#C2660D"),
    "open": ("#FFF1E0", "#C2660D"),
    "resolved": ("#E7F8EE", "#1E8E5A"),
    "closed": ("#F1F3F7", "#5B6B82"),
}


def inject_theme() -> None:
    st.markdown(
        f"""
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
        <style>
        html, body, [class*="css"] {{ font-family: 'Inter', sans-serif; }}
        .stApp {{ background-color: {BG}; }}
        section[data-testid="stSidebar"] {{ background-color: {NAVY}; }}
        section[data-testid="stSidebar"] * {{ color: #E7ECF5 !important; }}
        section[data-testid="stSidebar"] .stRadio label {{ color: #E7ECF5 !important; }}

        h1, h2, h3 {{ color: {NAVY}; font-weight: 700; }}
        p, span, label, div {{ color: {INK}; }}

        .cn-hero {{
            background: linear-gradient(135deg, {NAVY} 0%, {NAVY_LIGHT} 60%, {TEAL} 150%);
            border-radius: 16px; padding: 28px 32px; margin-bottom: 24px;
        }}
        .cn-hero h1 {{ color: white; margin: 0; font-size: 1.9rem; }}
        .cn-hero p {{ color: #CFE3E3; margin: 6px 0 0 0; font-size: 1.05rem; }}

        .cn-card {{
            background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 12px;
            padding: 18px 20px; margin-bottom: 14px;
        }}
        .cn-card h4 {{ margin: 0 0 10px 0; color: {NAVY}; }}
        .cn-muted {{ color: {MUTED}; font-size: 0.88rem; }}

        .cn-badge {{
            display: inline-block; padding: 3px 10px; border-radius: 999px;
            font-size: 0.78rem; font-weight: 600; letter-spacing: 0.02em;
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def status_badge(status: str) -> str:
    bg, fg = _STATUS_COLORS.get(status, ("#F1F3F7", MUTED))
    label = status.replace("_", " ").upper()
    return f'<span class="cn-badge" style="background:{bg};color:{fg};">{label}</span>'


# An agent run's persisted status is derived 1:1 from its Deterministic
# Verifier result (VERIFIED -> success, NEEDS_REVIEW -> partial, FAILED ->
# failed); the UI shows the verifier's vocabulary, which is what it means.
_VERIFICATION_LABELS = {"success": "verified", "partial": "needs_review", "failed": "failed"}


def verification_label(agent_status: str) -> str:
    return _VERIFICATION_LABELS.get(agent_status, agent_status)


def verification_badge(agent_status: str) -> str:
    return status_badge(verification_label(agent_status))


def render_hero(title: str, tagline: str) -> None:
    st.markdown(f'<div class="cn-hero"><h1>{title}</h1><p>{tagline}</p></div>', unsafe_allow_html=True)


def render_metric_card(label: str, value: str, sublabel: Optional[str] = None) -> None:
    sub_html = f'<div class="cn-muted">{sublabel}</div>' if sublabel else ""
    st.markdown(
        f'<div class="cn-card"><div class="cn-muted">{label}</div>'
        f'<div style="font-size:1.6rem;font-weight:700;color:{NAVY};">{value}</div>{sub_html}</div>',
        unsafe_allow_html=True,
    )
