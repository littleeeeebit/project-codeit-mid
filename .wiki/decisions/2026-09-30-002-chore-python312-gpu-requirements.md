---
scope: project
severity: preference
triggers: []
domain: ''
title: "Add Python 3.12 GPU dependencies to requirements.txt"
pr: 2
merged: 2026-09-30
branch: "chore/python312-gpu-requirements"
---

# Add Python 3.12 GPU dependencies to requirements.txt

What. Based on the plan documents (`docs/plan/end-to-end/`, `docs/rag/`), `requirements.txt` for the Python 3.12 + NVIDIA GPU environment has been added.

Why. Phase 1 core dependencies: PyMuPDF, pyhwp, kiwipiepy, rank_bm25, openai, tiktoken, numpy, streamlit, tzdata (`Asia/Seoul` for Windows) - GPU local reranker (`BAAI/bge-reranker-v2-m3`): torch `2.14.0+cu130`, sentence-transformers - As planned, versions are pinned to those installed after a smoke import on Windows. Docling, Tesseract OCR, Langfuse, FAISS, and Ragas are on hold until their necessity is confirmed through measurement as per the plan. pyhwp has been placed in the same environment instead of a separate converter environment; it will be separated if conflicts arise. …

Source. PR #2 · `chore/python312-gpu-requirements`
