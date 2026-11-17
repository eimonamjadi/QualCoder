# QualCoder

A local web app for **AI-assisted qualitative coding** of interview transcripts and other text, where every code the AI applies comes with receipts: the exact quote, a justification, and links to the codebook entries, examples and prompts it relied on.

Built for academic qualitative researchers and UX / market research teams. This is a working prototype.

**Website and browser demo:** https://eimonamjadi.github.io/QualCoder/ (the demo runs the real UI in your browser with the offline `mock` coder, not an AI model).

## Website (GitHub Pages)

`scripts/build_site.sh` builds a static site into `_site/`: a landing page (`site/index.html`) and, under `/demo/`, the app's own UI from `static/` with `site/demo-api.js` answering the `/api/*` calls in the browser (data in `localStorage`). `.github/workflows/pages.yml` builds and deploys it on every push to `main`. One-time setup: **Settings → Pages → Source: GitHub Actions**.

To preview locally: `./scripts/build_site.sh && python -m http.server -d _site`.

`demo-api.js` is a JavaScript port of the routes, retrieval, segmentation and mock coder, so if you change those in Python, change it too.

## What it does

1. **Codebook editor.** Define each code in your own words: definition, include-when, exclude-when, and example quotes (positive, or negative = "this is *not* the code").
2. **Documents.** Upload `.txt`, `.md`, `.docx` or `.pdf`, or paste text. Documents are split into passages at blank lines and at speaker labels (`P07:`, `Interviewer:`), with character offsets kept.
3. **Prompts.** Write your own instructions that steer coding ("don't code interviewer turns", "be conservative"). Each active prompt is shown to the model as `PROMPT:n`.
4. **AI coding with citations.** For each passage the app retrieves the most relevant codebook entries, examples and past corrections, sends them to the model with ref ids (`CODE:3`, `EX:12`, `PROMPT:1`), and asks for structured JSON: code, verbatim evidence quote, justification, cited refs, confidence.
5. **Checks, not just asks.** The app flags any quote that is not verbatim in the passage and any cited ref the model was not actually shown. The exact prompt and the raw reply of every run are saved and viewable ("view prompt & raw reply"), and you can preview a prompt before sending it.
6. **Review.** Accept, reject, or change the code on each suggestion, or add a code the model missed. Click a cited ref to see exactly what the model relied on.
7. **Learning from corrections.** Since Claude can't be fine-tuned here, corrections become the "training": a rejection is stored as a negative example for that code, a fix becomes a positive example for the new code, and (optionally) accepted codes become positive examples. Retrieval feeds them into later prompts, so the model sees your past decisions. Undo removes the example again.
8. **Exports.** `coded_segments.csv` (every application with quote, justification, refs, flags, review status) and `training_data.jsonl` (reviewed passages in chat format, a starting point for fine-tuning an open model later, e.g. LoRA with Unsloth).

## Model layer

Swappable from **Settings & export**:

| Provider | What it needs | Notes |
|---|---|---|
| `claude` (default) | `ANTHROPIC_API_KEY` | Claude Opus 5.5 (`claude-opus-5-5`), structured outputs, server-side refusal fallback on, codebook in a cached system block |
| `openai` | `OPENAI_API_KEY`, `QUALCODE_OPENAI_MODEL` | Chat Completions with a strict JSON schema |
| `ollama` | [Ollama](https://ollama.com) running locally, e.g. `ollama pull llama3.1` | Open-weight model, data stays on your machine |
| `mock` | nothing | Offline keyword matcher for demos and tests. **Not a model.** |

All providers fill the same JSON schema, so the rest of the app doesn't care which one ran.

## Run it

Python 3.10+.

```bash
git clone https://github.com/eimonamjadi/QualCoder.git && cd QualCoder
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # then put your key in .env (it is git-ignored)
uvicorn qualcode.main:app --reload
```

Open http://127.0.0.1:8000, go to **Documents**, click **Load sample codebook + transcript**, then **Review → Code all passages**.

No key yet? Choose `mock` under **Settings & export** to click through the whole workflow offline.

**Keys:** set them as environment variables or in `.env`. Never commit a key; `.env` and the database (`data/`) are in `.gitignore`.

Other settings (all optional, see `.env.example`): `QUALCODE_CLAUDE_MODEL`, `QUALCODE_CLAUDE_EFFORT` (default `medium`), `QUALCODE_CLAUDE_FALLBACKS=off`, `OLLAMA_HOST`, `QUALCODE_OLLAMA_MODEL`, `QUALCODE_DB`.

## Tests

```bash
pytest
```

19 tests cover segmentation, retrieval, the full API flow (load, code, accept, reject, re-code, undo, exports) with the mock provider, citation and quote validation, and the exact shape of the Claude request. **The Claude client is faked in tests**: they check what would be sent and how replies are handled, but no test calls the real API. The OpenAI and Ollama providers are untested.

## How it fits together

```
qualcode/
  main.py        FastAPI routes; serves the UI
  coding.py      retrieval -> prompt -> model -> citation checks -> store; JSONL export
  retrieval.py   BM25 over codes, examples and corrections (no embeddings service needed)
  providers.py   Claude / OpenAI / Ollama / mock behind one interface
  segment.py     file -> text -> passages with offsets
  db.py          SQLite (codes, examples, prompts, documents, passages, runs, applications)
static/          single-page UI, plain JS, no build step
samples/         a sample codebook and a synthetic interview transcript
tests/
```

**Why these choices:** FastAPI + SQLite + plain JS keeps it to one `pip install` and one command, and it can later be wrapped as a desktop app with Tauri. BM25 retrieval runs anywhere with no downloads; the `BM25Retriever` can be swapped for embeddings (e.g. Ollama `nomic-embed-text`) once codebooks grow.

## Not built yet

- Fine-tuning open models from your corrections (the JSONL export is the hook for it)
- Inter-rater reliability (e.g. Cohen's kappa between researcher and model, or between two coders)
- Multiple users / projects, auth, hosting
- Coding sub-passage spans by selecting text in the UI (codes apply to whole passages, with the quote highlighted)
- Embedding-based retrieval, inductive (open) coding suggestions

## Data and privacy

Everything is stored locally in `data/qualcode.db`. Passage text, codebook and examples are sent to whichever model provider you choose; use `ollama` if transcripts must not leave your machine, and check your IRB / data agreements before sending participant data to a cloud API.
