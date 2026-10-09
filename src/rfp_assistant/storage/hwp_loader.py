"""HWP/HWPX loader adapter: ingestion's parser when pyhwp fails. Loader order is NOT native document order."""
from collections.abc import Mapping
from importlib import import_module
from pathlib import Path

LOADER_ADAPTER_VERSION = "hwp-loader-adapter-2"  # bump when this adapter's output changes
LOADER_PACKAGES = ("langchain-hwp-hwpx-loader", "hwp-hwpx-parser")


def load_hwp_hwpx(original: Path) -> tuple[list[dict], list[dict], str | None]:
    """Return elements without inventing cells or coordinates.

    The loader gives the full body as one element, table text included, then notes, memos and
    links. Its separate table elements repeat that text (every table cell of AFSIS and MILE is
    in the body), so they are not requested. Without native order or cell structure it does not
    replace the XML walker: ingestion uses it only for an HWP pyhwp cannot read, and `fidelity
    run` judges it.

    Bad paths/extensions and dependency import failures propagate to the caller.
    Load failures return hwp_loader_failed. Invalid result shapes or conversion
    failures return hwp_loader_result_invalid and discard ALL partial elements.
    Empty lists/whitespace-only contents are valid but return hwp_empty_output.
    Metadata must be a mapping with string keys, even on an empty element;
    an empty mapping is accepted. No metadata values are invented or repaired.
    """
    if original.suffix.lower() not in {".hwp", ".hwpx"}:
        raise ValueError("Expected .hwp or .hwpx")
    if not original.is_file():
        raise FileNotFoundError(original)
    from langchain_hwp_hwpx import HwpHwpxLoader
    # The loader wraps its lazy parser import in HwpHwpxLoaderError. Check the
    # required parser here so an environment failure is not a document failure.
    import_module("hwp_hwpx_parser")

    try:
        documents = HwpHwpxLoader(
            original, mode="elements", include_tables=False, include_notes=True,
            include_memos=True, include_hyperlinks=True, include_images=False,
            include_extracted_at=False, on_invalid="raise", on_encrypted="raise",
            on_error="raise",
        ).load()
    except Exception as exc:
        return [], [{"code": "hwp_loader_error", "detail": type(exc).__name__}], "hwp_loader_failed"
    try:
        if not isinstance(documents, list):
            raise TypeError("Loader result must be a list")
        elements = []
        for index, document in enumerate(documents):
            content = document.page_content
            metadata = document.metadata
            if not isinstance(content, str):
                raise TypeError("page_content must be a string")
            if not isinstance(metadata, Mapping):
                raise TypeError("metadata must be a mapping")
            if any(not isinstance(key, str) for key in metadata):
                raise TypeError("metadata keys must be strings")
            meta = dict(metadata)
            if not content.strip():
                continue
            path = f"loader/e{index}"
            elements.append({"path": path, "kind": "paragraph",
                             "parent": None, "raw_text": content,
                             "location": {"format": f"{original.suffix[1:].lower()}_loader", "path": path,
                                          "section_path": [], "loader_metadata": meta,
                                          "order_basis": "loader_grouped_elements",
                                          "native_order_verified": False}})
    except Exception as exc:
        return [], [{"code": "hwp_loader_result_error", "detail": type(exc).__name__}], "hwp_loader_result_invalid"
    warnings = [{"code": "hwp_loader_structure_limited",
                 "detail": "One body element with table text inline; table cells, spans, pages and coordinates unavailable."}]
    return elements, warnings, None if elements else "hwp_empty_output"
