# Feature proposal — academic-researcher enhancements (2026-07)

**Status: DESIGN ONLY. Nothing below is implemented. Each candidate awaits
an explicit go/no-go [HUMAN DECISION].**

Produced under Strand E of the 2026-07 audit brief. Per candidate: problem
statement, proposed approach, effort estimate, zero-egress compliance
statement, and interaction with the capability-token gate.

---

## 1. Cross-session semantic search / chat over meeting history

**Problem.** `cli/search.py` is BM25 (lexical). A researcher asking "when
did we decide to switch the caching layer?" needs the words "caching layer"
to appear verbatim; paraphrases ("Redis migration") miss. There is no
conversational interface over history at all.

**Proposed approach.** Two stages, separably shippable:

1. *Semantic retrieval:* local embedding model (e.g. `bge-small-en-v1.5`
   ONNX/CTranslate2, ~130MB — downloaded by an extended `setup`, the one
   network-permitted step) + `sqlite-vec` (single-file, pure-C extension;
   preferred over chromadb — no server process, no telemetry surface, fits
   the plain-file ethos; chromadb pulls a large dependency tree and has
   had opt-out telemetry, which alone disqualifies it here). Index
   `*.summary.md` + `*.mom.md` + transcript chunks for sessions in
   PROPOSED/REVIEWED/APPLIED only — reusing the SB-6 privacy filter
   `cli/search.py::_is_indexable` already enforces for BM25. Hybrid
   ranking: BM25 ∪ vector, reciprocal-rank fusion.
2. *Chat:* a `/api/chat` endpoint doing plain RAG against the same local
   llama-server the pipeline already runs (`HttpLLMClient`), with the
   retrieved chunks in the prompt. Read-only; answers cite session ids.

**Effort.** Stage 1: ~3–4 days incl. index invalidation + tests. Stage 2:
~2–3 days incl. a minimal chat pane. `setup` extension: half a day.

**Zero-egress compliance.** COMPLIANT. Embedding model cached locally at
setup time; sqlite-vec is in-process; chat reuses the loopback llama-server.
The zero-egress import gate (`tests/security/test_zero_egress.py`) must
gain allowlist entries only if a new HTTP client appears — none is needed.

**Capability-token interaction.** None required: everything is read-only.
The chat tool must NOT be added to the MCP toolset (it is a human-facing
dashboard feature, not an agent capability); `todo.md` stays unreachable.

---

## 2. Speaker diarisation for seminar recordings

**Problem.** Multi-speaker seminars/reviews transcribe as an undifferentiated
stream (dual-track tagging only separates "You" vs "Others"). Action-item
ownership inference degrades badly with ≥3 speakers (code-review Personas
3/10).

**Proposed approach.** The plumbing already exists
(`transcribe/diarisation.py`, pyannote 3.1, best-effort, off by default).
What's actually missing, in order of value:
1. `setup --with-diarisation` to pre-fetch the pyannote pipeline (it is HF
   token-gated; the one-time `huggingface-cli login` is documented as a
   setup-step, never a runtime step) — closes the "enabled but weights not
   cached → silently degrades" gap;
2. a per-session UI toggle (Settings panel + pre-meeting modal) instead of
   the settings.toml-only flag;
3. speaker labels ("SPEAKER_00") surfaced in the transcript view with a
   rename control, feeding renamed labels into the extraction prompt.
Alternative if pyannote's gating/weight is unacceptable: `senko`/
`diart`-class lighter local pipelines — evaluate before committing.

**Effort.** 1: half a day. 2: 1 day. 3: 2–3 days (label propagation into
prompts + UI). Accuracy validation on real seminar audio: 1 day.

**Zero-egress compliance.** COMPLIANT with the same caveat as Whisper
models: weights must be fetched during `setup` only; runtime loads from
cache under `HF_HUB_OFFLINE=1` (audit finding A-5.2 recommends setting
that var in `diarisation.py` itself, independent of call order).

**Capability-token interaction.** None: diarisation runs inside
`transcribe_meeting`, which never touches `todo.md`.

---

## 3. Local Zotero-library cross-reference for project meetings

**Problem.** Research meetings reference literature ("the Chen 2025 paper");
minutes would be more useful if such mentions linked to the researcher's
own Zotero library entry.

**Proposed approach.** Read-only access to the *local* Zotero SQLite DB
(`~/Zotero/zotero.sqlite` — well-documented schema) opened with
`mode=ro&immutable=1` to respect Zotero's own lock. Post-extraction pass:
fuzzy-match capitalised title fragments / "Author (Year)" patterns from the
transcript + summary against `items`/`creators` tables; write matches into
`<sid>.literature.json` and render as a "References mentioned" section in
the MoM. Explicitly NOT the Zotero Web API (network) and NOT a write path
into Zotero.

**Effort.** ~2–3 days incl. schema fixture tests; graceful no-op when no
Zotero install exists (same best-effort pattern as mail_sync).

**Zero-egress compliance.** COMPLIANT — local file read only. It does read
a *new* sensitive local dataset, so it must be opt-in in settings
(`[integrations] zotero_enabled = false` by default), mirroring the
user-triggered-only stance taken for Outlook COM.

**Capability-token interaction.** None — writes only session artefacts
under `data/meetings/`, never `todo.md`.

---

## 4. Obsidian / Markdown-vault export

**Problem.** Everything is already plain Markdown, but locked into
`data/meetings/` layout; researchers living in Obsidian re-copy MoMs by
hand (Persona 8's export gap, still open).

**Proposed approach.** `meeting-agent export --vault <path>` (+ a per-
meeting "Export" button): render `<sid>.mom.md` + summary + accepted
action items into `<vault>/Meetings/<yyyy-mm-dd> <title>.md` with YAML
frontmatter (date, type, participants, session_id) and `[[wiki-links]]`
between chained sessions of the same slug. One-way, explicit, idempotent
(overwrite-same-file on re-export). No sync, no vault reads beyond
existence checks — Obsidian needs nothing installed.

**Effort.** ~1–2 days + tests. Cheapest high-value candidate.

**Zero-egress compliance.** COMPLIANT — writes local files to a
user-chosen path. Note: this is the first *write* outside `data/`; the
export path must be user-supplied per invocation or pinned in settings,
never derived from content, and the MCP layer must not gain an export
tool (CLI/web only), keeping the agent's write scope exactly as the audit
enumerated it (A-3).

**Capability-token interaction.** Not token-gated: it writes *outside*
the supervised `todo.md`/`projects` surface and only re-renders content a
human already reviewed. If the maintainer prefers symmetry, gating the
web export endpoint with a minted token is a 3-line addition — decide at
sign-off.

---

## 5. Weekly digest: does v2 already meet researcher needs?

**Assessment of the existing feature** (`cli/weekly_summary.py`): explicit
user trigger, 7-day window over `.summary.md`/`.actions.json`, one LLM
call, 6-hour cache, structured keys (key_decisions, recurring_topics,
open/completed counts, insight). For a working researcher this covers the
"what happened this week" ritual adequately.

**Gaps worth closing (extension, not rebuild):**
1. No trend across weeks — persist each digest (`weekly_summary-<isoweek>
   .json` instead of overwriting one file) and add week-over-week deltas
   (open-action trend, recurring topic persistence). ~1 day.
2. Session filtering is by artefact mtime, not state — a FAILED session's
   artefacts can leak into the digest; align with the SB-6 indexability
   filter. ~half a day (and it is arguably a small correctness bug today).
3. No export — solved by candidate 4 for free if that lands.

**Recommendation.** Do not build a new digest; land extensions 1–2.

**Zero-egress compliance.** COMPLIANT — unchanged local pipeline.

**Capability-token interaction.** None — read-only over artefacts.

---

## Suggested priority for the go/no-go discussion

| # | Candidate | Value/effort | Suggested |
|---|---|---|---|
| 4 | Obsidian export | High / ~1.5d | GO first |
| 1 | Semantic search (stage 1) | High / ~3.5d | GO second |
| 5 | Digest extensions | Medium / ~1.5d | GO (bundle with 4) |
| 2 | Diarisation UX | Medium / ~4.5d | GO if multi-speaker use is real |
| 3 | Zotero | Niche / ~2.5d | DEFER until asked for |

All five are zero-egress compatible; none touches the agent's write scope
or weakens the capability-token gate.
