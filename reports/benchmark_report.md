# ResearchForge benchmark report

This report is produced by `tools/benchmark.py` from `tools/benchmark_suite.json`.
It runs the real pipeline offline against synthetic, recorded-shape source payloads
and single-reviewer relevance judgements. **It measures the pipeline, not the live web.**

- Suite version: `1.0`
- Reviewer: ResearchForge project author (manual relevance judgement, one reviewer)
- Reviewed on: 2026-10-22
- Result: **PASS**

## Aggregate metrics

| Metric | Value |
| --- | --- |
| Cases | 3 |
| Records returned (canonical) | 5 |
| Precision (micro) | 0.8000 |
| Recall (micro) | 1.0000 |
| F1 (micro) | 0.8889 |
| Metadata accuracy (field-level) | 1.0000 (20 fields) |
| Duplicate-resolution recall | 1.0000 (1 expected pair(s)) |
| Unexpected merges | 0 |
| Identifier/link/timestamp preservation | 1.0000 |
| Required-resource recall | 1.0000 |
| Counterexample exclusion rate | 1.0000 |
| Claims stored | 32 |
| Unsupported-claim rate | 0.0000 |
| Exact insufficient-evidence message verified | True |

## Per case

| Case | Returned | P | R | F1 | Metadata | Suitability | Sufficiency | Failures |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `skin-lesion-image-datasets` | 2 | 1.0000 | 1.0000 | 1.0000 | 10/10 | req 2/2, excl 1/1 | sufficient | none |
| `fraud-detection-gnn-literature` | 3 | 0.6667 | 1.0000 | 0.8000 | 10/10 | req 2/2, excl 0/0 | sufficient | none |
| `no-dataset-worldwide` | 0 | n/a | n/a | n/a | n/a | req 0/0, excl 0/0 | insufficient_evidence | none |

## Per-case detail

### `skin-lesion-image-datasets`

- Question: Find publicly available image datasets of skin lesions for diagnosis
- Ground truth: 2 relevant, 1 counterexample(s) expected excluded
- Retrieved 2 record(s); 2 relevant, 0 not in the relevant set
- Sufficiency: `sufficient` — 2 candidate dataset(s) satisfy the stated requirements and passed metadata-level verification.
- Claims: 17, unsupported: 0
- URLs with no fixture (treated as transport failures, so their checks stay inconclusive): `HEAD huggingface.co/datasets/user/skin-lesion-dermoscopy/resolve/a1b2c3d4/.gitattributes`

### `fraud-detection-gnn-literature`

- Question: Find research on detecting fraudulent financial transactions with graph neural networks
- Ground truth: 2 relevant, 0 counterexample(s) expected excluded
- Retrieved 3 record(s); 2 relevant, 1 not in the relevant set
- Sufficiency: `sufficient` — 3 citable record(s) with source content are available.
- Claims: 15, unsupported: 0
- URLs with no fixture (treated as transport failures, so their checks stay inconclusive): `GET api.crossref.org/works/10.1000/med.2020.1`

### `no-dataset-worldwide`

- Question: Find a public dataset of confirmed crop yield measurements recorded on Mars colonies
- Ground truth: 0 relevant, 0 counterexample(s) expected excluded
- Retrieved 0 record(s); 0 relevant, 0 not in the relevant set
- Sufficiency: `insufficient_evidence` — There is insufficient data worldwide. No dataset.
- Claims: 0, unsupported: 0

## Honest limitations

- The corpus is synthetic. Each payload follows the documented response shape of its
  source API, but no request to the live APIs was made while building this report, so
  these numbers say nothing about real-world coverage or precision.
- Only one human reviewer judged relevance, so inter-annotator agreement is unknown.
- Probe URLs that were not fixtured are reported as transport failures; the pipeline
  keeps those checks inconclusive instead of assuming the link works.
- The suite is small (three cases). It is a regression harness for pipeline behaviour,
  not a statistically representative evaluation.
