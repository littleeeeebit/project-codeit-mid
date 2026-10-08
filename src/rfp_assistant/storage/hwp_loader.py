"""Read-only HWP/HWPX adapter. Loader order is NOT native document order."""
from collections.abc import Mapping
from importlib import import_module
from pathlib import Path


def load_hwp_hwpx(original: Path) -> tuple[list[dict], list[dict], str | None]:
    """Return preview elements without inventing cells or coordinates.

    Comparison/preview only: the loader groups the full body, then tables/notes. It cannot
    replace the structured XML walker without losing native paragraph/table order.

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
            original, mode="elements", include_tables=True, include_notes=True,
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
                             "location": {"format": original.suffix[1:].lower(), "path": path,
                                          "section_path": [], "loader_metadata": meta,
                                          "order_basis": "loader_grouped_elements",
                                          "native_order_verified": False}})
    except Exception as exc:
        return [], [{"code": "hwp_loader_result_error", "detail": type(exc).__name__}], "hwp_loader_result_invalid"
    warnings = [{"code": "hwp_loader_structure_limited",
                 "detail": "Grouped body/tables; native interleaving, cell spans, pages and coordinates unavailable."}]
    return elements, warnings, None if elements else "hwp_empty_output"
