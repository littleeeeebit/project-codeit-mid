# Primary source register for the RFP assistant

Sources were opened and checked on 2026-09-30. This register describes the reusable solution or concept; each topic page explains the proposed adaptation. Publisher capabilities and reported experiments do not establish performance on our Korean RFP corpus. Prices and software behavior should be checked again before implementation.

## Documents and ingestion

| Source | Reusable piece | Limit for this project |
| --- | --- | --- |
| [pyhwp introduction](https://pyhwp.readthedocs.io/en/latest/intro.html) | HWP v5 parser, experimental converters, compatibility and license information | Old documented runtime support; actual audit includes two failures |
| [pyhwp processor](https://pyhwp.readthedocs.io/en/latest/hwp5proc.html) | Structured XML export and preview/body stream inspection | XML must be checked; export is not table QA |
| [pyhwp converters](https://pyhwp.readthedocs.io/en/latest/converters.html) | Existing text/HTML/ODT conversion commands | Local `hwp5txt` samples lost table contents |
| [Hancom OWPML model](https://github.com/hancom-io/hwpx-owpml-model) | Official HWPX XML model reference | Does not itself convert supplied binary HWP files |
| [PyMuPDF text recipes](https://pymupdf.readthedocs.io/en/latest/recipes-text.html) | Blocks, reading order, and table extraction | Korean tables still need original-page comparison |
| [PyMuPDF OCR](https://pymupdf.readthedocs.io/en/latest/recipes-ocr.html) | Page-level local OCR and result reuse | Requires separate OCR installation and appropriate language data |
| [Tesseract model data](https://github.com/tesseract-ocr/tessdata_best) | Official OCR language models | OCR quality and numeric/code errors need checks |
| [Docling](https://github.com/docling-project/docling) | Local document layout, tables, and OCR alternative | No assumption of complete HWP support or free model runtime |

## Chunking, retrieval, and reranking

| Source | Reusable piece | Limit for this project |
| --- | --- | --- |
| [Unstructured chunking](https://docs.unstructured.io/open-source/core-functionality/chunking) | Element, title, table, and oversized-element handling | Documented size controls are character based |
| [LlamaIndex recursive retrieval](https://developers.llamaindex.ai/python/framework/integrations/retrievers/recursive_retriever_nodes/) | Small-node retrieval with parent reference expansion | Parent expansion still needs a final token budget |
| [Kiwi Python interface](https://github.com/bab2min/kiwipiepy) | Korean tokenizer and domain dictionary | Code/acronym and compound behavior need domain inspection |
| [rank_bm25](https://github.com/dorianbrown/rank_bm25) | Small local BM25 implementation | Analyzer must be supplied consistently by the application |
| [Nori tokenizer](https://www.elastic.co/docs/reference/elasticsearch/plugins/analysis-nori-tokenizer) | Korean compounds and user dictionary in an existing search engine | Running a search server is optional infrastructure |
| [Microsoft RRF reference](https://learn.microsoft.com/en-us/azure/search/hybrid-search-ranking) | Rank-based sparse/dense fusion | Constants and weights require our own evaluation |
| [BGE-M3 embedding card](https://huggingface.co/BAAI/bge-m3) | Multilingual dense and learned sparse retrieval | Not a BM25 implementation or the named reranker |
| [BGE reranker v2 M3 card](https://huggingface.co/BAAI/bge-reranker-v2-m3) | Multilingual query-passage cross-encoder | No measured Korean RFP latency or quality here |
| [Jina multilingual reranker card](https://huggingface.co/jinaai/jina-reranker-v2-base-multilingual) | Reranker alternative and longer-passage windows | Noncommercial licensing and model-specific runtime constraints |

## Domain and evaluation

| Source | Reusable piece | Limit for this project |
| --- | --- | --- |
| [MIT RFx capstone, May 2025](https://ctl.mit.edu/sites/ctl.mit.edu/files/theses/zaunicknastasja_170045_5333547_SCM15_Zaunick_Paredes_CapstoneReport.pdf) | Procurement chatbot with metadata and prompt/evaluation workflow | Historical supplier RFx differs from Korean public-sector RFPs |
| [CUAD paper](https://arxiv.org/abs/2103.06268) | Expert-reviewed evidence labels for contract document tasks | English legal contracts are an adjacent domain |
| [Ragas question generation](https://docs.ragas.io/en/stable/concepts/test_data_generation/rag/) | Question taxonomy and multi-source scenarios | Generated candidates still require independent approval |
| [Ragas paper](https://arxiv.org/abs/2309.15217) | Stage-specific RAG evaluation concepts | Automated scores alone do not certify business facts |
| [ARES paper](https://arxiv.org/abs/2311.09476) | Human-calibrated automated relevance/faithfulness evaluation | Adopting the full learned judge is unnecessary for the first delivery |

## Generation, costs, and interfaces

| Source | Reusable piece | Limit for this project |
| --- | --- | --- |
| [OpenAI prompt guidance](https://developers.openai.com/api/docs/guides/prompt-engineering) | Instructions, boundaries, examples, and relevant evidence | Grounding still needs measured source support |
| [OpenAI structured output](https://developers.openai.com/api/docs/guides/structured-outputs) | Strict output schema | Structure does not guarantee factual correctness |
| [GPT-4o mini](https://developers.openai.com/api/docs/models/gpt-4o-mini) | Proposed low-cost answer baseline and published prices | Academy-key access and task adequacy untested |
| [GPT-4.1 mini](https://developers.openai.com/api/docs/models/gpt-4.1-mini) | A higher-cost comparison candidate and prices | No automatic escalation proposed |
| [Embedding small](https://developers.openai.com/api/docs/models/text-embedding-3-small) | Proposed inexpensive dense index and prices | Evaluate Korean domain retrieval |
| [OpenAI streaming usage](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create) | Final streamed usage and interrupted-stream limitation | Exact usage is not available for every partial stream |
| [OpenAI Usage and Costs](https://platform.openai.com/docs/api-reference/usage/costs) | Organization-level reconciliation | Scope, authorization, and data freshness need confirmation |
| [OpenAI spend limits](https://developers.openai.com/api/docs/guides/spend-limits) | Alerts versus hard limits and delayed enforcement | Monthly limits differ from the project's cumulative allowance |
| [OpenAI prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching) | Reusable prompt-prefix pricing behavior | Model-dependent; no savings assumed in admission checks |
| [OpenAI Batch](https://developers.openai.com/api/docs/guides/batch) | Discounted asynchronous offline jobs | Completion may take 24 hours |
| [LiteLLM budgets](https://docs.litellm.ai/docs/proxy/users) | Existing team/member budget gateway | Database required; deployment adds operational work |
| [Langfuse costs](https://langfuse.com/docs/observability/features/token-and-cost-tracking) | Existing per-call generation/embedding observability | Observability alone is not atomic budget enforcement |
| [SQLite transactions](https://www.sqlite.org/lang_transaction.html) | Short atomic write transaction for reservations | Single shared database; do not hold locks over network calls |
| [Streamlit navigation](https://docs.streamlit.io/develop/api-reference/navigation/st.navigation) | Separate workflow pages | Navigation visibility does not replace role authorization |
| [Streamlit login](https://docs.streamlit.io/develop/api-reference/user/st.login) | Native login when an identity provider is already configured | Requires provider secrets and does not supply this project's role/allowance policy |
| [Streamlit fragments](https://docs.streamlit.io/develop/api-reference/execution-flow/st.fragment) | Timed partial rerun for a usage widget | Verify polling during long requests and across sessions |

## Evidence status

Local evidence is recorded in [the dataset audit](evidence/dataset-audit.json) and explained in [the project page](project-and-data.md). Research used `pyhwp 0.1b15`, `PyMuPDF 1.28.0`, and Python 3.13.9 for those inspection runs. Structured extraction lengths count text nodes, not final cleaned/chunked/indexed content. There are no retrieval benchmark scores or paid API results in this research.

A 2026 paper titled “Retrieval-Augmented Generation for Procurement Validation” was found during searching, but its publisher's full text returned access errors. It was not used to justify this architecture or any performance claim. The successfully opened MIT capstone supplies the closer-domain implementation evidence.
