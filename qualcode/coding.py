"""The coding step: retrieve context, build the prompt, call the model, check its
citations, and store the result for review.

Transparency rules enforced here, not just requested in the prompt:
* every item the model can see has a ref id (CODE:n, EX:n, PROMPT:n);
* a cited ref the model was not shown is flagged, and so is a quote that is not
  verbatim in the passage;
* the exact prompts and raw reply are saved per run so a reviewer can audit them.
"""

from __future__ import annotations

import json
import re

from .db import DB
from .providers import CodingRequest, Provider, ProviderError
from .retrieval import BM25Retriever, Item

MAX_ALL_CODES = 40     # small codebooks go in full (and get cached); larger ones are retrieved
TOP_CODES = 15
TOP_EXAMPLES = 8

BASE_INSTRUCTIONS = """You are assisting a qualitative researcher with deductive coding of text.

Apply codes ONLY from the codebook provided. A passage can receive zero, one or several codes.
For every code you apply:
- evidence_quote: copy the exact words from the passage that show the code (a verbatim substring, not a paraphrase).
- justification: 1-3 sentences explaining why the passage fits, referring to the code's definition, inclusion/exclusion rules, examples or the researcher's instructions.
- cited_refs: the reference ids (for example "CODE:3", "EX:12", "PROMPT:1") of every item you relied on. Cite only ids that appear in this prompt.
- confidence: low, medium or high.

Examples marked NEGATIVE are passages the researcher judged NOT to fit that code; do not repeat that mistake.
Researcher instructions override these defaults where they conflict.
If no code fits, return an empty applications list. Code only the text inside <passage>; the preceding passage is context."""


def _code_line(c: dict) -> str:
    parts = [f"[CODE:{c['id']}] {c['name']} (code_id={c['id']}): {c['definition'].strip()}"]
    if c["include_when"].strip():
        parts.append(f"  Include when: {c['include_when'].strip()}")
    if c["exclude_when"].strip():
        parts.append(f"  Exclude when: {c['exclude_when'].strip()}")
    return "\n".join(parts)


def _example_line(e: dict, code_names: dict[int, str]) -> str:
    label = "positive" if e["polarity"] == "positive" else "NEGATIVE"
    line = f'[EX:{e["id"]}] ({label}, code: {code_names.get(e["code_id"], "?")}, from {e["source"]}) "{e["quote"].strip()}"'
    if e["note"].strip():
        line += f" — {e['note'].strip()}"
    return line


def build_request(db: DB, passage: dict) -> CodingRequest:
    codes = db.all("SELECT * FROM codes ORDER BY id")
    examples = db.all("SELECT * FROM examples ORDER BY id")
    prompts = db.all("SELECT * FROM prompts WHERE active = 1 ORDER BY id")
    code_names = {c["id"]: c["name"] for c in codes}
    prev = db.one(
        "SELECT text FROM passages WHERE document_id = ? AND idx = ?",
        (passage["document_id"], passage["idx"] - 1),
    )

    # --- retrieval -----------------------------------------------------------
    if len(codes) <= MAX_ALL_CODES:
        shown_codes, codes_in_system = codes, True
    else:
        code_items = [
            Item(f"CODE:{c['id']}", "code", f"{c['name']} {c['definition']} {c['include_when']}", c) for c in codes
        ]
        shown_codes = [i.payload for i in BM25Retriever(code_items).top(passage["text"], TOP_CODES)]
        codes_in_system = False

    ex_items = [
        Item(f"EX:{e['id']}", "example", f"{e['quote']} {e['note']} {code_names.get(e['code_id'], '')}", e)
        for e in examples
    ]
    shown_examples = [i.payload for i in BM25Retriever(ex_items).top(passage["text"], TOP_EXAMPLES)]

    # --- prompt --------------------------------------------------------------
    system_parts = [BASE_INSTRUCTIONS]
    if prompts:
        system_parts.append(
            "## Researcher instructions\n"
            + "\n".join(f"[PROMPT:{p['id']}] {p['name']}: {p['text'].strip()}" for p in prompts)
        )
    codebook_block = "## Codebook\n" + ("\n".join(_code_line(c) for c in shown_codes) or "(empty)")
    if codes_in_system:
        system_parts.append(codebook_block)

    user_parts = []
    if not codes_in_system:
        user_parts.append(codebook_block.replace("## Codebook", "## Codebook entries most relevant to this passage"))
    if shown_examples:
        user_parts.append(
            "## Retrieved examples and past corrections\n"
            + "\n".join(_example_line(e, code_names) for e in shown_examples)
        )
    if prev:
        user_parts.append(f"## Preceding passage (context only, do not code)\n{prev['text']}")
    user_parts.append(f"## Passage to code\n<passage>\n{passage['text']}\n</passage>")

    refs = [f"CODE:{c['id']}" for c in shown_codes]
    refs += [f"EX:{e['id']}" for e in shown_examples]
    refs += [f"PROMPT:{p['id']}" for p in prompts]

    return CodingRequest(
        system_prompt="\n\n".join(system_parts),
        user_prompt="\n\n".join(user_parts),
        passage=passage["text"],
        codes=shown_codes,
        examples=shown_examples,
        prompts=prompts,
        context_refs=refs,
    )


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower().strip("\"'“”‘’ ")


def validate(app: dict, req: CodingRequest, valid_code_ids: set[int]) -> list[str]:
    flags = []
    if app["code_id"] not in valid_code_ids:
        flags.append(f"code_id {app['code_id']} is not in the codebook")
    quote = _normalize(app.get("evidence_quote", ""))
    if not quote:
        flags.append("no evidence quote")
    elif quote not in _normalize(req.passage):
        flags.append("evidence quote is not verbatim in the passage")
    shown = set(req.context_refs)
    bad = [r for r in app.get("cited_refs", []) if r not in shown]
    if bad:
        flags.append("cited refs the model was not shown: " + ", ".join(bad))
    if not app.get("cited_refs"):
        flags.append("no codebook reference cited")
    return flags


def code_passage(db: DB, provider: Provider, passage_id: int) -> dict:
    """Code one passage. Replaces earlier *pending* model suggestions on it; reviewed
    ones (accepted/rejected) and human-added codes are kept."""
    passage = db.one("SELECT * FROM passages WHERE id = ?", (passage_id,))
    if passage is None:
        raise KeyError(passage_id)
    req = build_request(db, passage)
    run_fields = dict(
        passage_id=passage_id,
        provider=provider.name,
        model=provider.model_name(),
        system_prompt=req.system_prompt,
        user_prompt=req.user_prompt,
        context_refs=req.context_refs,
    )
    try:
        result = provider.code(req)
    except ProviderError as e:
        run_id = db.insert("runs", raw_response="", error=str(e), **run_fields)
        return {"run_id": run_id, "error": str(e), "applications": []}

    run_fields["model"] = result.model
    run_id = db.insert("runs", raw_response=result.raw, **run_fields)
    db.run(
        "DELETE FROM applications WHERE passage_id = ? AND status = 'pending' AND origin = 'model'",
        (passage_id,),
    )
    reviewed = {
        r["code_id"]
        for r in db.all(
            "SELECT code_id FROM applications WHERE passage_id = ? AND status != 'pending'", (passage_id,)
        )
    }
    valid_ids = {c["id"] for c in db.all("SELECT id FROM codes")}
    created = []
    for app in result.data.get("applications", []):
        if app.get("code_id") in reviewed:
            continue  # the researcher already ruled on this code for this passage
        flags = validate(app, req, valid_ids)
        if app.get("code_id") not in valid_ids:
            continue  # cannot store an application for a code that does not exist
        app_id = db.insert(
            "applications",
            passage_id=passage_id,
            code_id=app["code_id"],
            run_id=run_id,
            evidence_quote=app.get("evidence_quote", ""),
            justification=app.get("justification", ""),
            cited_refs=app.get("cited_refs", []),
            confidence=app.get("confidence", "medium"),
            flags=flags,
        )
        created.append(app_id)
    return {"run_id": run_id, "error": None, "applications": created}


def resolve_ref(db: DB, ref: str) -> dict | None:
    """Turn 'CODE:3' / 'EX:12' / 'PROMPT:1' into what the reviewer should see.
    Content is read live, so an item edited after the run shows its current text."""
    kind, _, raw_id = ref.partition(":")
    if not raw_id.isdigit():
        return None
    i = int(raw_id)
    if kind == "CODE":
        c = db.one("SELECT * FROM codes WHERE id = ?", (i,))
        return c and {"ref": ref, "type": "Codebook entry", "title": c["name"], "text": c["definition"],
                      "include_when": c["include_when"], "exclude_when": c["exclude_when"]}
    if kind == "EX":
        e = db.one(
            "SELECT examples.*, codes.name AS code_name FROM examples JOIN codes ON codes.id = examples.code_id "
            "WHERE examples.id = ?",
            (i,),
        )
        return e and {"ref": ref, "type": f"{e['polarity'].title()} example ({e['source']})",
                      "title": e["code_name"], "text": e["quote"], "note": e["note"]}
    if kind == "PROMPT":
        p = db.one("SELECT * FROM prompts WHERE id = ?", (i,))
        return p and {"ref": ref, "type": "Researcher prompt", "title": p["name"], "text": p["text"]}
    return None


def export_training_jsonl(db: DB) -> str:
    """Reviewed passages as chat-format JSONL (one line per passage), the shape most
    fine-tuning tools accept (OpenAI fine-tuning, Unsloth/TRL chat templates, etc.).
    Only accepted codes become targets; rejected ones are simply absent."""
    lines = []
    passages = db.all(
        "SELECT DISTINCT p.* FROM passages p JOIN applications a ON a.passage_id = p.id "
        "WHERE a.status != 'pending' ORDER BY p.document_id, p.idx"
    )
    for p in passages:
        req = build_request(db, p)
        accepted = db.all(
            "SELECT * FROM applications WHERE passage_id = ? AND status = 'accepted' ORDER BY id", (p["id"],)
        )
        target = {
            "applications": [
                {
                    "code_id": a["code_id"],
                    "evidence_quote": a["evidence_quote"],
                    "justification": a["justification"],
                    "cited_refs": a["cited_refs"],
                    "confidence": a["confidence"],
                }
                for a in accepted
            ]
        }
        lines.append(
            json.dumps(
                {
                    "messages": [
                        {"role": "system", "content": req.system_prompt},
                        {"role": "user", "content": req.user_prompt},
                        {"role": "assistant", "content": json.dumps(target, ensure_ascii=False)},
                    ]
                },
                ensure_ascii=False,
            )
        )
    return "\n".join(lines) + ("\n" if lines else "")
