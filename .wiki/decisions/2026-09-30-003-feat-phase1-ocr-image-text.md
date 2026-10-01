---
scope: project
severity: contract
triggers: ["지연", "latency", "텔레메트리", "턴 조립", "api 조립"]
domain: api
title: "Phase 1 RFP assistant and text OCR in images"
pr: 3
merged: 2026-09-30
branch: "feat/phase1-ocr-image-text"
---

# Phase 1 RFP assistant and text OCR in images

What. Phase 1 vertical slices and raster image area OCR added.

Why. Collection: HWP uses pyhwp XML worker (formula scripts, image captions, table of contents lines), PDF uses PyMuPDF blocks and tables - Fidelity check: Bi-directional comparison of Hancom print text layers and extraction results (34 automatic verifications, 58 automatic flags) - Structural chunking (table titles stay with rows), Kiwi BM25 search, evidence-based answers behind a budget gate (gpt-6-luna, $5 limit), gold inspection queue, Streamlit UI 1. PaddleOCR-VL 1.6 (local GPU, revision fixed) reads all 514 raster image areas of HWP printouts and PDF originals 2. Fallback judgment: decoding loop, token exhaustion (2,048), average token probability less than 0.90, ink estimate (font size connected components × 0.39) compared to …

Source. PR #3 · `feat/phase1-ocr-image-text`
