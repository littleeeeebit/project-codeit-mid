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

If a page renders text but extraction yields blank text, replacement glyphs, or implausible content, investigate missing character mappings and consider OCR. Changing a PDF string's encoding will not repair missing glyph-to-Unicode mappings. [PyMuPDF's OCR guidance](https://pymupdf.readthedocs.io/en/latest/recipes-ocr.html) uses separately installed Tesseract and recommends reusing the OCR result. OCR only the affected pages, once, and cache by source hash plus OCR configuration.

Use Korean and English OCR language data and inspect digits, dates, `0/O`, requirement codes, and currency symbols. The [official Tesseract language repository](https://github.com/tesseract-ocr/tessdata_best) supplies model files. Do not replace a good text layer with OCR simply because the document is Korean.

[Docling](https://github.com/docling-project/docling) is an alternative for PDF layout, tables, and local OCR. Trial it on pages where the lighter extractor demonstrably fails. Its documented format support does not establish reliable binary HWP ingestion. Avoid downloading and running heavyweight models on all documents before this comparison.

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
