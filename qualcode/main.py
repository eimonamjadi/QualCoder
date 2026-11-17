"""FastAPI app: JSON API + a single-page UI served from /static."""

from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import coding
from .db import DB
from .providers import DEFAULT_PROVIDER, PROVIDERS, Provider
from .segment import extract_text, segment

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
SAMPLES = ROOT / "samples"


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Minimal .env reader so keys can live in an untracked file. Real environment
    variables win over the file."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.split(" #", 1)[0].strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


load_dotenv()


# ----------------------------------------------------------------------- models
class CodeIn(BaseModel):
    name: str = Field(min_length=1)
    definition: str = ""
    include_when: str = ""
    exclude_when: str = ""


class ExampleIn(BaseModel):
    quote: str = Field(min_length=1)
    note: str = ""
    polarity: Literal["positive", "negative"] = "positive"


class PromptIn(BaseModel):
    name: str = Field(min_length=1)
    text: str = ""
    active: bool = True


class TextDocIn(BaseModel):
    name: str = Field(min_length=1)
    text: str = Field(min_length=1)


class CodeRunIn(BaseModel):
    provider: str | None = None


class ReviewIn(BaseModel):
    action: Literal["accept", "reject", "recode", "undo"]
    new_code_id: int | None = None
    note: str = ""


class HumanCodeIn(BaseModel):
    code_id: int
    evidence_quote: str = ""
    note: str = ""


class SettingsIn(BaseModel):
    provider: str | None = None
    learn_from_accepted: bool | None = None


# ----------------------------------------------------------------------- app
def create_app(db_path: str | None = None, providers: dict[str, Provider] | None = None) -> FastAPI:
    db = DB(db_path or os.environ.get("QUALCODE_DB", str(ROOT / "data" / "qualcode.db")))
    registry: dict[str, Provider] = providers or {name: cls() for name, cls in PROVIDERS.items()}
    app = FastAPI(title="QualCoder", version="0.1.0")
    app.state.db = db

    def current_provider(name: str | None = None) -> Provider:
        name = name or db.get_setting("provider", DEFAULT_PROVIDER)
        if name not in registry:
            raise HTTPException(400, f"Unknown provider '{name}'")
        return registry[name]

    def need(row, what="Not found"):
        if row is None:
            raise HTTPException(404, what)
        return row

    # ---------------------------------------------------------------- codebook
    @app.get("/api/codes")
    def list_codes():
        codes = db.all("SELECT * FROM codes ORDER BY id")
        examples = db.all("SELECT * FROM examples ORDER BY id")
        for c in codes:
            c["examples"] = [e for e in examples if e["code_id"] == c["id"]]
        return codes

    @app.post("/api/codes")
    def create_code(body: CodeIn):
        if db.one("SELECT id FROM codes WHERE name = ?", (body.name,)):
            raise HTTPException(409, "A code with that name exists")
        return {"id": db.insert("codes", **body.model_dump())}

    @app.put("/api/codes/{code_id}")
    def update_code(code_id: int, body: CodeIn):
        need(db.one("SELECT id FROM codes WHERE id = ?", (code_id,)))
        db.update("codes", code_id, **body.model_dump())
        return {"ok": True}

    @app.delete("/api/codes/{code_id}")
    def delete_code(code_id: int):
        db.run("DELETE FROM codes WHERE id = ?", (code_id,))
        return {"ok": True}

    @app.post("/api/codes/{code_id}/examples")
    def add_example(code_id: int, body: ExampleIn):
        need(db.one("SELECT id FROM codes WHERE id = ?", (code_id,)))
        return {"id": db.insert("examples", code_id=code_id, source="user", **body.model_dump())}

    @app.delete("/api/examples/{example_id}")
    def delete_example(example_id: int):
        db.run("DELETE FROM examples WHERE id = ?", (example_id,))
        return {"ok": True}

    @app.get("/api/codebook/export")
    def export_codebook():
        codes = list_codes()
        out = [
            {
                "name": c["name"],
                "definition": c["definition"],
                "include_when": c["include_when"],
                "exclude_when": c["exclude_when"],
                "examples": [
                    {"quote": e["quote"], "note": e["note"], "polarity": e["polarity"]}
                    for e in c["examples"]
                    if e["source"] == "user"
                ],
            }
            for c in codes
        ]
        return Response(
            json.dumps({"codes": out}, indent=2, ensure_ascii=False),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="codebook.json"'},
        )

    @app.post("/api/codebook/import")
    async def import_codebook(file: UploadFile = File(...)):
        try:
            data = json.loads(await file.read())
        except json.JSONDecodeError:
            raise HTTPException(400, "Codebook must be JSON")
        return {"imported": _import_codebook(db, data)}

    # ---------------------------------------------------------------- prompts
    @app.get("/api/prompts")
    def list_prompts():
        return db.all("SELECT * FROM prompts ORDER BY id")

    @app.post("/api/prompts")
    def create_prompt(body: PromptIn):
        return {"id": db.insert("prompts", name=body.name, text=body.text, active=int(body.active))}

    @app.put("/api/prompts/{prompt_id}")
    def update_prompt(prompt_id: int, body: PromptIn):
        need(db.one("SELECT id FROM prompts WHERE id = ?", (prompt_id,)))
        db.update("prompts", prompt_id, name=body.name, text=body.text, active=int(body.active))
        return {"ok": True}

    @app.delete("/api/prompts/{prompt_id}")
    def delete_prompt(prompt_id: int):
        db.run("DELETE FROM prompts WHERE id = ?", (prompt_id,))
        return {"ok": True}

    @app.get("/api/base-instructions")
    def base_instructions():
        return {"text": coding.BASE_INSTRUCTIONS}

    # ---------------------------------------------------------------- documents
    def _store_document(name: str, text: str) -> int:
        doc_id = db.insert("documents", name=name, text=text)
        for i, p in enumerate(segment(text)):
            db.insert("passages", document_id=doc_id, idx=i, text=p.text, start_char=p.start, end_char=p.end)
        return doc_id

    @app.get("/api/documents")
    def list_documents():
        return db.all(
            """SELECT d.id, d.name, d.created_at,
                      (SELECT COUNT(*) FROM passages p WHERE p.document_id = d.id) AS passages,
                      (SELECT COUNT(*) FROM applications a JOIN passages p ON p.id = a.passage_id
                         WHERE p.document_id = d.id AND a.status = 'pending') AS pending,
                      (SELECT COUNT(*) FROM applications a JOIN passages p ON p.id = a.passage_id
                         WHERE p.document_id = d.id AND a.status = 'accepted') AS accepted
               FROM documents d ORDER BY d.id"""
        )

    @app.post("/api/documents/upload")
    async def upload_document(file: UploadFile = File(...), name: str = Form("")):
        try:
            text = extract_text(file.filename or "upload.txt", await file.read())
        except ValueError as e:
            raise HTTPException(400, str(e))
        if not text.strip():
            raise HTTPException(400, "No text found in that file")
        return {"id": _store_document(name or file.filename or "Untitled", text)}

    @app.post("/api/documents/text")
    def paste_document(body: TextDocIn):
        return {"id": _store_document(body.name, body.text)}

    @app.get("/api/documents/{doc_id}")
    def get_document(doc_id: int):
        doc = need(db.one("SELECT id, name, created_at FROM documents WHERE id = ?", (doc_id,)))
        passages = db.all("SELECT * FROM passages WHERE document_id = ? ORDER BY idx", (doc_id,))
        apps = db.all(
            """SELECT a.*, c.name AS code_name, r.provider, r.model
               FROM applications a JOIN passages p ON p.id = a.passage_id
               JOIN codes c ON c.id = a.code_id LEFT JOIN runs r ON r.id = a.run_id
               WHERE p.document_id = ? ORDER BY a.id""",
            (doc_id,),
        )
        last_runs = db.all(
            """SELECT r.passage_id, r.id, r.error FROM runs r JOIN passages p ON p.id = r.passage_id
               WHERE p.document_id = ? AND r.id IN (SELECT MAX(id) FROM runs GROUP BY passage_id)""",
            (doc_id,),
        )
        run_by_passage = {r["passage_id"]: r for r in last_runs}
        for p in passages:
            p["applications"] = [a for a in apps if a["passage_id"] == p["id"]]
            p["last_run"] = run_by_passage.get(p["id"])
        doc["passages"] = passages
        return doc

    @app.delete("/api/documents/{doc_id}")
    def delete_document(doc_id: int):
        db.run("DELETE FROM documents WHERE id = ?", (doc_id,))
        return {"ok": True}

    # ---------------------------------------------------------------- coding
    @app.post("/api/passages/{passage_id}/code")
    def code_passage(passage_id: int, body: CodeRunIn | None = None):
        provider = current_provider(body.provider if body else None)
        if not provider.is_configured():
            raise HTTPException(400, f"The {provider.name} provider is not configured. See the README.")
        try:
            return coding.code_passage(db, provider, passage_id)
        except KeyError:
            raise HTTPException(404, "Passage not found")

    @app.get("/api/passages/{passage_id}/preview")
    def preview_prompt(passage_id: int):
        """Exactly what the model would be sent for this passage, without calling it."""
        p = need(db.one("SELECT * FROM passages WHERE id = ?", (passage_id,)))
        req = coding.build_request(db, p)
        return {"system_prompt": req.system_prompt, "user_prompt": req.user_prompt, "context_refs": req.context_refs}

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: int):
        return need(db.one("SELECT * FROM runs WHERE id = ?", (run_id,)))

    @app.get("/api/refs/{ref}")
    def get_ref(ref: str):
        return need(coding.resolve_ref(db, ref), "That reference no longer exists")

    # ---------------------------------------------------------------- review
    def _learn(code_id: int, quote: str, note: str, polarity: str, source: str) -> int:
        return db.insert("examples", code_id=code_id, quote=quote, note=note, polarity=polarity, source=source)

    @app.post("/api/applications/{app_id}/review")
    def review(app_id: int, body: ReviewIn):
        a = need(db.one("SELECT * FROM applications WHERE id = ?", (app_id,)))
        passage = db.one("SELECT * FROM passages WHERE id = ?", (a["passage_id"],))
        if body.action == "undo":
            if a["learned_example_id"]:
                db.run("DELETE FROM examples WHERE id = ?", (a["learned_example_id"],))
            if a["origin"] == "human":
                db.run("DELETE FROM applications WHERE id = ?", (app_id,))
            else:
                db.update("applications", app_id, status="pending", reviewer_note="", learned_example_id=None)
            return {"ok": True}

        if a["status"] != "pending":
            raise HTTPException(409, "Already reviewed; undo first")

        quote = a["evidence_quote"] or passage["text"]
        if body.action == "accept":
            ex_id = None
            if db.get_setting("learn_from_accepted", "1") == "1":
                ex_id = _learn(a["code_id"], quote, body.note or a["justification"], "positive", "accepted")
            db.update("applications", app_id, status="accepted", reviewer_note=body.note, learned_example_id=ex_id)
            return {"ok": True}

        # reject and recode both teach the model that this passage is NOT this code
        reason = body.note or "Researcher rejected this code for this passage."
        ex_id = _learn(a["code_id"], passage["text"], reason, "negative", "correction")
        db.update("applications", app_id, status="rejected", reviewer_note=body.note, learned_example_id=ex_id)
        if body.action == "recode":
            if body.new_code_id is None or not db.one("SELECT id FROM codes WHERE id = ?", (body.new_code_id,)):
                raise HTTPException(400, "new_code_id must be an existing code")
            add_human_code(a["passage_id"], HumanCodeIn(code_id=body.new_code_id, evidence_quote=quote, note=body.note))
        return {"ok": True}

    @app.post("/api/passages/{passage_id}/applications")
    def add_human_code(passage_id: int, body: HumanCodeIn):
        passage = need(db.one("SELECT * FROM passages WHERE id = ?", (passage_id,)))
        need(db.one("SELECT id FROM codes WHERE id = ?", (body.code_id,)), "Unknown code")
        quote = body.evidence_quote or passage["text"]
        note = body.note or "Applied by the researcher."
        ex_id = _learn(body.code_id, quote, note, "positive", "correction")
        app_id = db.insert(
            "applications",
            passage_id=passage_id,
            code_id=body.code_id,
            origin="human",
            evidence_quote=quote,
            justification=note,
            cited_refs=[],
            confidence="high",
            status="accepted",
            reviewer_note=body.note,
            learned_example_id=ex_id,
        )
        return {"id": app_id}

    # ---------------------------------------------------------------- settings
    @app.get("/api/settings")
    def get_settings():
        return {
            "provider": db.get_setting("provider", DEFAULT_PROVIDER),
            "learn_from_accepted": db.get_setting("learn_from_accepted", "1") == "1",
            "providers": [
                {"name": name, "model": p.model_name(), "configured": p.is_configured()}
                for name, p in registry.items()
            ],
        }

    @app.put("/api/settings")
    def put_settings(body: SettingsIn):
        if body.provider is not None:
            current_provider(body.provider)  # validates
            db.set_setting("provider", body.provider)
        if body.learn_from_accepted is not None:
            db.set_setting("learn_from_accepted", "1" if body.learn_from_accepted else "0")
        return get_settings()

    # ---------------------------------------------------------------- exports
    @app.get("/api/export/segments.csv")
    def export_segments():
        rows = db.all(
            """SELECT d.name AS document, p.idx AS passage, p.start_char, p.end_char, c.name AS code,
                      a.status, a.origin, a.evidence_quote, a.justification, a.cited_refs, a.confidence,
                      a.flags, a.reviewer_note, r.model
               FROM applications a JOIN passages p ON p.id = a.passage_id
               JOIN documents d ON d.id = p.document_id JOIN codes c ON c.id = a.code_id
               LEFT JOIN runs r ON r.id = a.run_id ORDER BY d.id, p.idx, a.id"""
        )
        buf = io.StringIO()
        if rows:
            w = csv.DictWriter(buf, fieldnames=list(rows[0]))
            w.writeheader()
            for r in rows:
                r["cited_refs"] = " ".join(r["cited_refs"])
                r["flags"] = "; ".join(r["flags"])
                w.writerow(r)
        return Response(
            buf.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="coded_segments.csv"'},
        )

    @app.get("/api/export/training.jsonl")
    def export_training():
        return PlainTextResponse(
            coding.export_training_jsonl(db),
            media_type="application/jsonl",
            headers={"Content-Disposition": 'attachment; filename="training_data.jsonl"'},
        )

    # ---------------------------------------------------------------- samples
    @app.post("/api/samples/load")
    def load_samples():
        data = json.loads((SAMPLES / "codebook.json").read_text())
        n = _import_codebook(db, data)
        for p in data.get("prompts", []):
            if not db.one("SELECT id FROM prompts WHERE name = ?", (p["name"],)):
                db.insert("prompts", name=p["name"], text=p["text"], active=1)
        doc_id = None
        transcript = SAMPLES / "interview_transcript.txt"
        if not db.one("SELECT id FROM documents WHERE name = ?", (transcript.name,)):
            doc_id = _store_document(transcript.name, transcript.read_text())
        return {"codes_imported": n, "document_id": doc_id}

    # ---------------------------------------------------------------- UI
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    return app


def _import_codebook(db: DB, data: dict) -> int:
    n = 0
    for c in data.get("codes", []):
        existing = db.one("SELECT id FROM codes WHERE name = ?", (c["name"],))
        if existing:
            continue
        code_id = db.insert(
            "codes",
            name=c["name"],
            definition=c.get("definition", ""),
            include_when=c.get("include_when", ""),
            exclude_when=c.get("exclude_when", ""),
        )
        for e in c.get("examples", []):
            db.insert(
                "examples",
                code_id=code_id,
                quote=e["quote"],
                note=e.get("note", ""),
                polarity=e.get("polarity", "positive"),
                source="user",
            )
        n += 1
    return n


app = create_app() if os.environ.get("QUALCODE_NO_AUTOAPP") != "1" else None
