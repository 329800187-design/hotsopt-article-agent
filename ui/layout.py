from __future__ import annotations

import streamlit as st


def page_header(kicker: str, title: str, description: str = "") -> None:
    st.markdown(f'<div class="rc1-kicker">{kicker}</div>', unsafe_allow_html=True)
    st.title(title)
    if description:
        st.caption(description)


def three_panel():
    return st.columns([1.1, 2.2, 1.1], gap="large")
