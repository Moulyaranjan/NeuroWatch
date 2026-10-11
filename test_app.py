import pytest

try:
	import streamlit as st
except ModuleNotFoundError:  # Streamlit is intentionally optional for the FastAPI app.
	pytest.skip("Streamlit is not installed in this environment; the FastAPI app is the primary runtime.", allow_module_level=True)


def test_streamlit_placeholder():
	st.title("NeuroWatch Test")
	st.write("Streamlit is working!")
