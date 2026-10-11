`_prepare_paid` handles three modes:
- single: `prepare_answer` over the scope.
- compare: `_prepare_compare` runs one `retrieve` per document at full single-document limits.
- corpus: `prepare_answer(all_documents=True)`, naming only documents that contributed evidence.
With `allow_paid=True`, an uncached dense query vector may be bought through the gateway under the request's dispatch guard. `_priced` counts prompt tokens exactly and estimates the maximum reservation; an unknown rate leaves the estimate None, so admission later refuses `unknown_rate`. Empty evidence ends as `insufficient_evidence` with no generation charge.