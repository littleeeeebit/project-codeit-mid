The packing loop walks the final ranking and admits each chunk as an `EvidenceUnit` with ID `E{n}`, the doc and source hash, the extraction, the element IDs, the quote and the location. A chunk is refused when:
- it carries a different requirement code than the one asked (`other_requirement_code`);
- the unit limit is reached (`unit_limit`);
- its span overlaps a chunk already admitted (`duplicate_span`);
- it does not fit the token budget (`token_budget`).

The first unit may use `evidence_max_tokens`; later units use `evidence_target_tokens`. The linked sibling pieces of a split element follow their chunk, nearest first, up to `LINKED_EXTRA_UNITS`. A piece left out is reported as `linked_evidence_missing:`.