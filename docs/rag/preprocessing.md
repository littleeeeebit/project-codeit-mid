# Complete Korean HWP and PDF preprocessing

Proposed route: treat the originals as the content source, the CSV as metadata plus a potentially incomplete preview, and preserve an immutable original alongside structured extracted elements. Decode correctly, check extraction coverage, and retain table relationships as three separate requirements. None substitutes for another.

## HWP and HWPX

[pyhwp](https://pyhwp.readthedocs.io/en/latest/intro.html) is a HWP v5 parser with experimental conversion tools. Its documented Python compatibility is old; the audit used an isolated temporary installation on the current Python 3.13 environment. Trial success does not establish general compatibility. Record the exact package versions and keep the converter environment separate from the app. Its published license is AGPLv3; assess that dependency before distribution.

Use [structured XML export](https://pyhwp.readthedocs.io/en/latest/hwp5proc.html), rather than `hwp5txt`, as the primary trial:

```text
hwp5proc xml --output parsed.xml source.hwp
```

Walk the document's paragraph and table hierarchy in source order. Keep text inside `TableCell`, row/column indices and spans, section identity, and element ordinals. Count content inside nested cells exactly once. A flat walk of every `Text` element is useful for the audit's volume checks, but does not constitute the final table-preserving ingestion algorithm.

The documented `PrvText` stream uses UTF-16LE and is a preview, not the full body. Inspect it to diagnose supplied text, never use it as the complete index. HWP binary records, compression, and controls belong to the parser; applying `decode('utf-8')` to the original HWP file is incorrect.

Quarantine password-protected, unsupported, malformed, or incomplete documents. A zero exit code is insufficient: require nonempty well-formed XML and expected content. The audit encountered malformed XML from this converter. Do not repair it with `errors='ignore'` or silently drop invalid characters; locate the failure or route the document through a verified alternative.

The all-file trial left two recovery cases: 한국농어촌공사 AFSIS Cambodia, whose exported XML was malformed, and 대전대학교 MILE, whose style record raised a UTF-16 decoding error. The other 94 HWP outputs parsed, but still require table/content review. Neither failure is evidence that the original Korean document cannot be read by its native application.

Fallback: when an approved Hancom installation is available, export the affected HWP to PDF or HWPX, then verify it visually against the original. Tool availability and automated batch conversion have not been checked. [Hancom's OWPML model](https://github.com/hancom-io/hwpx-owpml-model) is a reference for the XML structure; it is not a drop-in HWP converter. HWPX is a different container and needs its own ZIP/XML route if later supplied. The present corpus contains no HWPX files.

## PDF route

Use [PyMuPDF text blocks and table extraction](https://pymupdf.readthedocs.io/en/latest/recipes-text.html) on every page. Its documentation describes reading-order controls and `Page.find_tables()`. Reading-order sorting alone cannot prove correct table associations, especially across pages or merged cells. Keep page number, bounding boxes, and the original text; verify units and footnotes against the rendered page.

If a page renders text but extraction yields blank text, replacement glyphs, or implausible content, investigate missing character mappings and use the OCR route below. Changing a PDF string's encoding will not repair missing glyph-to-Unicode mappings. Do not replace a good text layer with OCR simply because the document is Korean.

## Text inside images

Decided 2026-09-30: a local OCR model reads every image region; only the regions it fails on go to Gemini. No general VLM runs locally. The corpus is digital, so images are embedded pictures, not scans.

1. Regions. Raster images on the page (`page.get_image_info()`, at least `IMAGE_MIN_AREA` of the page): for HWP on the Hancom print, for PDF on the original. Vector drawings such as WMF are not raster images; the print renders their text into the text layer, which the fidelity check compares. Each region is rendered clean at 150 dpi.
2. Local OCR: PaddleOCR-VL 1.6, prompt `OCR:`, greedy, stopped on a loop (12 identical lines, or 48 lines with at most 8 distinct).
3. Fallback test, per region, from signals available without the truth: the loop stop fired; the output ran out of tokens (2,048) before the model ended it; mean token probability below 0.90; or the output is longer than 20 times the ink estimate, or longer than 5 times and over 500 characters. The ink estimate is 0.39 characters per glyph-sized connected component (correlation 0.99 with the true length on text regions).
4. Gemini (`gemini-3.5-flash-lite`, $0.30 / $2.50 per 1M input / output tokens) reads only flagged regions, and its text replaces the OCR text for that region. Spend has its own hard cap of $0.50, separate from the OpenAI budget. A call is refused when its worst-case cost would pass the cap, and a call whose outcome or usage figures are unknown is charged at worst case. Only one process at a time may spend from the ledger: the reader holds an exclusive lock on it from start to finish, so the cap check and the reservation cannot interleave with another run's. Only one OCR run at a time, local-only included, may write the cache, so one run's checkpoint cannot overwrite another's paid reads. A blocked prompt or an unfinished answer is a failure that stays unresolved for the next run, not an empty read.
5. Output: element kind `image_text`, with page, box and engine (`paddleocr-vl-1.6` or the Gemini model) in its location. Cached by source hash plus OCR configuration; a cached read is reused only for the same image digest, and a finished read of that digest anywhere is preferred over an unfinished one in place. A local read cached before truncation was recorded is read again. At ingest only reads of regions the rendering still has (same page, box and pixels) are merged; the rest are reported as `ocr_stale` until OCR runs again. The OCR text goes after the element holding the print text above the image, or before the one holding the text below; where elements have pages (PDF, the Hancom print), that element must be on the anchor's page, and a picture with no such element is reported as `ocr_unplaced`. On a rotated page, boxes stay in unrotated coordinates and only the render is rotated. The extraction fingerprint covers the added text, where it was placed and the placement version, so moved OCR text is a new revision. OCR text has no independent witness and is labelled machine-read wherever it is cited.

Benchmark behind these choices: 80 print regions whose text layer is the truth.

| Model | Korean 3-gram recall | 3-gram precision | CER | s/region |
|---|---|---|---|---|
| PaddleOCR-VL 1.6 | 0.78 | 0.85 | 0.31 | 6.1 |
| GLM-OCR | 0.62 | 0.64 | 0.35 | 8.1 |

The thresholds were first fitted on those text regions (0.95 confidence, 0.8–1.5 times the ink). On the corpus's real images they flagged 365 of 438 distinct images: charts, diagrams, maps, logos and scanned forms behave differently. They were refitted on 50 random real regions, with Gemini's read as the reference and "failed" meaning an order-free character F1 below 0.8 against it. 16 of 50 local reads failed, as runaway repetition, loops, garbled map or diagram labels, or near-empty output. The rule above flags all 16, plus 4 good reads. A shortfall against the ink is not used, because contents pages with dotted leaders read perfectly at 0.2–0.6 of the estimate. Across the corpus it flags 193 of 463 distinct images. The fit rests on 50 samples; confident misreads of single characters are not caught.

[Docling](https://github.com/docling-project/docling) is an alternative for PDF layout and tables. Its documented format support does not establish reliable binary HWP ingestion.

## Korean preservation and output contract

1. Read the supplied CSV using `utf-8-sig`; retain multiline quoted fields with a CSV parser.
2. Write all new text, JSON, and Markdown using UTF-8 without BOM; configure subprocess decoding and terminal output explicitly.
3. Preserve raw extracted text. Apply NFC to a separate searchable representation so decomposed Hangul is searchable without rewriting evidence.
4. Normalize line endings and obvious repeated whitespace inside a cell or paragraph. Keep boundaries, negation, punctuation-bearing IDs, units, and table structure.
5. Save extraction method/version, source hash, completeness state, and source locations. Never invent HWP page numbers from paragraph counts.

For HWP without trustworthy pagination, citations should name section, requirement ID, and table/cell ordinal and offer the original download. Show PDF physical page number separately from a printed page label when they differ.

## Acceptance before indexing

Every one of the 100 files must have an ingestion status. Check start, middle, and end content; detailed requirement IDs beyond the contents page; representative budget, submission, and requirement tables; and source-viewer navigation. Compare extracted amounts and identifiers to the originals, not to generated summaries. U+FFFD count, Hangul count, and character length are diagnostics rather than an automatic pass.

Keep failed documents visible with their reason and suppress unsupported full-document answers. The product must distinguish “not present in the original,” “original not fully ingested,” and “retrieval did not find it.” See [evaluation](evaluation.md) for how those states affect scoring.
