# ResearchForge

A verified global research and dataset intelligence platform.

It discovers sources through official public APIs, normalises them into one schema,
verifies their identity and links, extracts evidence from text that was actually
retrieved, and reports only what it can support.

> **Central rule of this project: never fabricate data, sources, citations, dataset
> counts, research findings, experiments or conclusions. When sufficient evidence
> cannot be found or verified, the platform says so explicitly.**

---

## 1. Requirements

| Item | Requirement |
| --- | --- |
| OS | Windows (the batch scripts) — the Python app itself is cross-platform |
| Python | 3.10 or newer (the code uses PEP 604 union type syntax) |
| Third-party packages | `fastapi`, `uvicorn` (see `requirements.txt`) |
| Everything else | Python standard library: HTTP client, SQLite, hashing, DOCX/LaTeX export, tests |
| Network | Outbound HTTPS to the public research APIs listed in §5 |
| API keys | **None required.** All sources work anonymously at reduced rate limits. |

The application was developed and verified on **Python 3.15.1 / Windows**. No
other interpreter version was executed while building this project.

---

## 2. Quick start

```bat
setup.bat     :: one-time: create .venv, install dependencies, create .env, init the database
run.bat       :: start the server at http://127.0.0.1:8000
test.bat      :: run the unit suite and the offline benchmark harness
```

Then open <http://127.0.0.1:8000/>.

### Manual start (no batch scripts)

```bat
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
.venv\Scripts\python.exe -m app.main --no-browser --port 8000
```

Or with uvicorn directly:

```bat
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

### Tests and evaluation

```bat
python -m unittest discover -s tests -t .      :: 195 unit tests, offline
python tools\benchmark.py                      :: offline benchmark harness
```

The benchmark writes `reports/benchmark_report.md` and `reports/benchmark_report.json`.

---

## 3. Configuration

Copy `.env.example` to `.env`. Every value is optional.

| Variable | Purpose |
| --- | --- |
| `RF_HOST`, `RF_PORT`, `RF_OPEN_BROWSER` | Server binding |
| `RF_DATA_DIR`, `RF_DB_PATH`, `RF_CACHE_DIR`, `RF_DOWNLOAD_DIR` | Storage locations |
| `RF_HTTP_TIMEOUT`, `RF_HTTP_MAX_RETRIES`, `RF_HTTP_BACKOFF_BASE` | Outbound HTTP behaviour |
| `RF_CACHE_TTL_SECONDS`, `RF_MAX_CONCURRENT_REQUESTS` | Response cache and concurrency |
| `RF_CONTACT_EMAIL`, `RF_USER_AGENT` | Polite identification (some APIs grant better limits with a `mailto`) |
| `SEMANTIC_SCHOLAR_API_KEY`, `HUGGINGFACE_TOKEN`, `DATAGOV_API_KEY` | Optional; raise rate limits / unlock gated metadata |
| `RF_DEFAULT_SEARCH_DEPTH`, `RF_DEFAULT_PAGE_SIZE`, `RF_MAX_RESULTS_PER_SOURCE` | Search defaults |
| `RF_LLM_BASE_URL`, `RF_LLM_API_KEY`, `RF_LLM_MODEL`, `RF_LLM_TIMEOUT` | Optional OpenAI-compatible endpoint |

**Secrets are never returned by the API.** `/api/health` and `/api/meta` report
only whether a credential is configured (`credentials_present`), never the value.

**The optional LLM is never a source of retrieved facts.** With no key configured
the whole pipeline runs deterministically; the parser, query planner, relevance
filter, deduplicator, verifier, evidence extractor, gap finder and manuscript
builder are all rule-based.

---

## 4. Architecture

```
app/
  config.py          settings + .env loading
  db.py              SQLite (WAL) schema, 14 tables, schema version
  http_client.py     stdlib HTTP client: retries, backoff, timeouts, on-disk cache, redirect capture
  logging_utils.py   structured JSON logs + in-memory recent-log ring
  main.py            FastAPI app, static frontend mount, CLI entry point
  api/routes.py      typed API surface (pydantic v2 contracts)
  sources/           9 source adapters + registry + NormalizedRecord schema
  services/
    parser.py        natural-language request -> ResearchSpec
    planning.py      synonym/query expansion, per-source plan, filters
    orchestration.py the run: search -> filter -> dedupe -> persist -> verify -> evidence -> sufficiency
    dedupe.py        DOI, then title+year, then fuzzy title similarity (>= 0.87, same year)
    verification.py  identifier / registrar / metadata / license / link checks
    evidence.py      document retrieval, passage extraction, sufficiency gate
    gaps.py          evidence matrix and gap discovery
    compatibility.py dataset compatibility and quality checks
    downloads.py     resumable downloads, SHA-256, zip CRC, provenance manifest
    manuscript.py    guided 12-section manuscript workspace
    docx_writer.py   DOCX (Office Open XML) writer built from the standard library
    jobs.py          background jobs with persisted state and cooperative cancellation
    store.py         persistence helpers shared by the orchestrator and the API
    lexicon.py       domain / modality / synonym vocabulary
    llm.py           optional, clearly optional, never a fact source
  static/            no-build vanilla JS frontend (index.html, app.js, styles.css)
tests/               195 unit tests + a stdlib ASGI test harness
tools/               benchmark.py + benchmark_suite.json (offline evaluation harness)
```

The frontend is a hand-drawn "notebook and stickers" theme: paper dot-grid background,
wobbly ink borders, hard offset shadows, washi tape on the modal, wavy underlines and
per-panel pop-in. The three webfonts (Shantell Sans, Caveat, Nunito) load from Google
Fonts; if the network is unavailable the stylesheet falls back to Comic Sans MS /
Segoe Print / Segoe UI, so the layout degrades rather than breaks. The landing page's
five decorative illustrations are generated locally (`app/static/img/land-*.jpg`) and
served as static assets by the same FastAPI app — no external image requests are made.

### Frontend shell: landing page, workspace profile, dashboard, record profile

The UI is a three-step shell, all client-side, with no accounts and no server-side session:

1. **Landing page** (`#view-landing`) — what the app does, the nine sources it searches,
   the ten pipeline stages, and the exact insufficient-evidence line quoted live from
   `GET /api/meta`. "Enter workspace" moves to the gate.
2. **Workspace gate** (`#gate`) — a display name and an optional role, stored only in this
   browser under `localStorage["rf.profile"]`. No password, no email, nothing is sent to
   any research source. A returning visitor skips the gate and lands straight on the
   dashboard. The header **Profile** button reopens, edits or forgets it; forgetting the
   profile deletes no research, because projects and records live in SQLite, not the profile.
3. **Dashboard** (`#panel-dashboard`) — the default panel. Live counts, three example
   questions that prefill the search box, six quick-action cards, the ten-stage pipeline in
   one glance, links to the latest run and the newest records, and the recent-project list.
   The detailed search options now sit inside a collapsed `<details>` so the first-run
   screen is one input and one button.

Clicking any paper or dataset — from Datasets, Literature, the evidence matrix or the
dashboard — opens a **full-page record profile** (`#panel-record`) instead of the old
cramped modal: title header, verification and dataset-state badges, DOI/source/PDF links,
the complete metadata table, dataset fields, download state, every verification check, and
the extracted evidence passages with locators. `← Back` returns to the panel it was opened
from.

`#panel-howto` ("How it works") prints all ten pipeline stages with the exact module that
implements each one, so the claim is checkable in the source tree rather than asserted.

### Pipeline guarantees

- Records keep their **original identifiers, source URL, license and retrieval timestamp**.
- **Deduplication is deterministic**: exact normalised DOI → normalised title + year
  → title-token Jaccard similarity ≥ 0.87 within the same year. Each merge records its reason.
- **Verification** resolves identifiers against Crossref/DataCite and probes links with
  `HEAD` requests. Inaccessible sources are labelled *inaccessible*, never assumed good.
- **Evidence** is a passage plus a document identifier, a locator, a URL and a retrieval date.
  A claim without those is not stored.
- The **search ledger** records one row per (source, query, page) with result counts,
  new-record counts, duration, status and any exclusion reasons.

---

## 5. Sources

| Adapter | Source | Kind | Families |
| --- | --- | --- | --- |
| `openalex` | OpenAlex | papers + dataset-typed works | literature |
| `crossref` | Crossref | papers | literature |
| `semantic_scholar` | Semantic Scholar | papers | literature |
| `europepmc` | Europe PMC | papers (life sciences) | life_sciences |
| `zenodo` | Zenodo | datasets + papers | datasets, repository |
| `datacite` | DataCite | datasets + papers | datasets |
| `huggingface` | Hugging Face Hub | datasets | datasets |
| `uci` | UCI Machine Learning Repository | datasets | datasets |
| `datagov` | Data.gov (CKAN + GSA Catalog API v4) | datasets | government_open_data, datasets |

Only official APIs are used. Adapters are isolated: one failing or rate-limited
source never blocks the others, and the failure is recorded rather than hidden.

---

## 6. API surface

```
GET    /                                   the frontend
GET    /static/*                            frontend assets
GET    /api/health                          readiness, version, schema version, configuration
GET    /api/meta                            policies, dataset states, the insufficient-evidence message
GET    /api/adapters                        adapter inventory, credential presence, families
GET    /api/logs                            recent structured log lines
POST   /api/research/plan                   interpretation + plan, no retrieval
POST   /api/research                        start a background run (202)
POST   /api/research/sync                   run synchronously and return the result
GET    /api/jobs, /api/jobs/{id}            job state
POST   /api/jobs/{id}/cancel                cooperative cancellation
GET    /api/projects, /api/projects/{id}    projects
DELETE /api/projects/{id}                   delete a project
GET    /api/projects/{id}/snapshot          everything for the workspace in one call
GET    /api/projects/{id}/ledger            the search ledger for the latest run
GET    /api/projects/{id}/records           records (filterable, with duplicates)
GET    /api/records/{id}                    one record
GET    /api/records/{id}/evidence           stored evidence/claims
GET    /api/records/{id}/verifications      verification checks
GET    /api/projects/{id}/evidence-matrix   evidence matrix
GET    /api/projects/{id}/gaps              gap candidates
POST   /api/projects/{id}/gaps              recompute gaps
POST   /api/datasets/{id}/download          start a download (202)
POST   /api/datasets/{id}/download/sync     download synchronously
GET    /api/downloads, /api/downloads/{id}  download state
POST   /api/downloads/purge-partial         remove partial files
POST   /api/projects/{id}/compatibility     compatibility check (202, or /sync)
GET    /api/projects/{id}/compatibility     stored compatibility report
GET    /api/projects/{id}/manifest          provenance manifest
GET    /api/projects/{id}/manifest/download download the manifest
GET    /api/projects/{id}/manuscript        the manuscript sections
POST   /api/projects/{id}/manuscript        rebuild (202, or /sync)
GET    /api/projects/{id}/manuscript/export export as docx | md | tex | json
```

Errors use one shape:

```json
{"error": {"status": 404, "message": "...", "path": "/api/..."}}
```

---

## 7. Verified results

Everything in this section was **executed on 2026-10-22** on this machine. Nothing
here is projected, estimated or assumed.

### 7.1 Unit tests — PASS

```
Ran 195 tests in 16.747s
OK
```

### 7.2 Offline benchmark / evaluation harness — PASS

`python tools\benchmark.py` runs the real pipeline against human-reviewed ground
truth and synthetic payloads written in the documented response shape of each API.

```
Running benchmark suite 1.0 (3 cases)
  [PASS] skin-lesion-image-datasets: 2 record(s), sufficiency=sufficient
  [PASS] fraud-detection-gnn-literature: 3 record(s), sufficiency=sufficient
  [PASS] no-dataset-worldwide: 0 record(s), sufficiency=insufficient_evidence

precision=0.8 recall=1.0 f1=0.8889
metadata_accuracy=1.0 dedupe_pair_recall=1.0 unsupported_claim_rate=0.0
All benchmark expectations satisfied.
```

### 7.3 Live end-to-end — verified

The real server was started with `python -m app.main --no-browser --port 8137` and
exercised over HTTP against the live public APIs:

| Check | Result |
| --- | --- |
| Startup | all 9 adapters available, no source reported unavailable |
| `GET /api/health` | 200 |
| `GET /api/meta` | 200, returns the exact insufficient-evidence message |
| `GET /api/adapters` | 200, 9 adapters |
| `POST /api/research/plan` | 200, spec + expanded queries |
| `GET /` | 200, `text/html`, 14 655 bytes |
| `GET /static/app.js`, `/static/styles.css` | 200, correct media types |
| `GET /api/logs`, `/api/jobs`, `/api/projects`, `/api/downloads` | 200 |
| **Live `POST /api/research/sync`** (OpenAlex + Crossref) | 200, `completed`; 2 queries / 2 pages, 10 candidates, 7 verified; 10 records persisted carrying real DOIs, licenses and access states |
| Live search ledger | 2 entries for 2 queries |
| Live verification | 2 records verified (5 checks each) under `verify_limit=2` |
| **Live insufficient-evidence path** | 200, `insufficient_evidence`, message exactly `There is insufficient data worldwide. No dataset.`, `exact_message_used=true`, 0 candidates |
| `POST /api/projects/{id}/manuscript/sync` | 200; sections include `needs_user_input` where the researcher must supply input |
| `GET .../manuscript/export` | 200 for all four formats: `docx` (valid ZIP/OOXML, `PK` magic), `md`, `tex`, `json` |
| `POST /api/projects/{id}/compatibility/sync` | 200, per-check statuses and evidence |

### 7.4 The deployment scripts were executed, not just written

| Script | Result |
| --- | --- |
| `setup.bat` | exit 0 — created `.venv`, installed `fastapi`/`uvicorn`, created `.env` from `.env.example`, initialised the database |
| `run.bat` | started the server on the requested port using the `.venv` interpreter; `GET /api/health` returned 200 |
| `test.bat` | exit 0 — `Ran 195 tests ... OK` followed by the benchmark harness reporting "All benchmark expectations satisfied." |

### 7.5 Implementation defect found and fixed during benchmarking

The benchmark harness exposed a real end-to-end bug that the unit tests had missed:
`NormalizedRecord` had **no `doi` field**, so `records.doi` was always `NULL`. That
silently degraded DOI-based deduplication, DOI verification and manuscript
references. Fixed by adding `doi` to the schema and populating it in every adapter
that already computed one (OpenAlex, Crossref, DataCite, Europe PMC, Semantic
Scholar, Zenodo). Re-running the suite confirmed `dedupe_pair_recall` went from
**0.0 → 1.0** and `metadata_accuracy` from **0.8667 → 1.0**, with all 195 unit tests
still passing and live records now carrying real DOIs.

---

## 8. Honest limitations

These are real limitations. They are not hedged, and they are not fixed by wording.

1. **The benchmark corpus is synthetic.** Each fixture follows the documented
   response shape of its source API, but no request to the live APIs was made while
   *building the report*. The benchmark measures the **pipeline**, not the live
   internet. No metric in it describes real-world coverage or precision.
2. **Only one reviewer judged relevance**, so inter-annotator agreement is unknown.
3. **The benchmark has three cases.** It is a regression harness for pipeline
   behaviour, not a statistically representative evaluation.
4. **Live verification is a spot check, not a survey.** Only OpenAlex, Crossref,
   Zenodo, Hugging Face and DataCite were exercised live. `semantic_scholar`,
   `europepmc`, `uci` and `datagov` were covered by unit tests but were **not**
   driven against their live APIs in this session.
5. **The download manager was not exercised against a real file.** Download,
   resume, SHA-256 and zip-CRC paths are covered by unit tests only.
6. **The frontend was opened in a browser preview, but not exhaustively clicked.**
   The page rendered, the JavaScript bootstrapped, and it loaded a real project's
   meta, adapter inventory, snapshot, dataset list, literature list and logs — all
   `200`, with no browser errors reported. Panels that require a button press
   (evidence matrix, gap discovery, manuscript assembly, exports) were verified at
   the HTTP layer instead, and their rendering code is unchanged.
   The theme also adds one new external dependency: the Google Fonts stylesheet.
   Without network access the page falls back to locally available handwriting
   fonts and remains fully usable.
7. **No language model was used.** The live runs used `use_llm=false` and no LLM key
   was configured. The deterministic path is what was verified; an LLM-assisted
   parse is untested here.
8. **Coverage is never internet-wide.** The platform searches the sources it has
   adapters for and says so in every scope statement. It does not and cannot claim
   global coverage.
9. **Rate limits are real.** Anonymous access means `semantic_scholar` in particular
   is heavily throttled; failures are recorded in `sources_failed`, not hidden.

### What is deliberately *not* built

- **No one-click paper generator.** The manuscript workspace writes the twelve
  guided sections and marks the ones that need researcher input as
  `needs_user_input`; it does not invent results, experiments or conclusions.
- **No fabricated fallbacks.** When evidence is insufficient the platform returns
  `There is insufficient data worldwide. No dataset.` instead of the closest guess.

---

## 9. Data and privacy

- Everything is stored locally under `data/` in SQLite (WAL mode) plus the HTTP cache.
- No telemetry is sent anywhere. The only outbound traffic is the source API requests.
- `data/` and `.env` should not be committed to version control.
