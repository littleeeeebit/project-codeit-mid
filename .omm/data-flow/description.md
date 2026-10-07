How corpus bytes become cited evidence, and where each form is persisted. Every hop is identified by a hash:
- source_hash: the original bytes;
- extraction_id: the parser fingerprint plus the output;
- index_version: the configuration hash;
- the dense set version;
- payload_hash: the normalized text plus model and dimensions;
- evidence_id: per request.