# MIKA — Case Chat: implementation plan

> Reviewed ✓ (bulletproof — 2026-06-26, round 3) — **SUFFICIENT** (re-reviewed after the /eagleye purpose-refocus;
> 3 recommended polish items folded in: mm-backstop now STRIPS the token instead of nuking the answer; the
> no-`--add-dir`/no-`--permission-mode` scope invariant is asserted + tested; `GET /api/chat/availability` gates
> the flag-off button). Implement behind
> `MIKA_CHAT_ENABLED=0`. Round-1 blocker fixed (load the NORMALIZED report payload, not raw `_load_report` —
> top-level `patient.findings` is empty on most studies; data lives under `agent.summary.patient`). Safety is
> now prompt + a deterministic last-writer mm backstop + process-level no-`--add-dir` scoping. Live `claude -p`
> needs a human gate (nested-session hang). Additive/flag-dark — merge is inert.
>
> **Language: the chat answers in whatever language the patient asks — Claude handles it natively. NO
> translation layer, no gate, no Arabic plumbing.** (Build note: frontend line anchors drifted after the i18n
> work — re-grep at build; `_resolve_claude_bin` is at `agent_runner.py:82`.)

**Goal.** Let a patient ask questions about **their one completed study**, answered **concisely**,
**grounded only in their report**, and **never** as medical advice. Ships as a dismissible overlay
drawer on the Read screen — zero change to the existing no-scroll layout or the read pipeline.

**Hard constraints (the "surgical / 0-errors" bar).**
- **Additive only.** New files + one new endpoint + one new frontend component. **No edits** to
  `AgentRunner.run()`, the report builders, the read pipeline, or any existing endpoint.
- **Feature-flagged dark.** `MIKA_CHAT_ENABLED` (default `0`). Trigger button and endpoint are inert
  until flipped, so merging cannot affect today's behavior.
- **Same locked-down posture** as the rest of MIKA: `JOB_ID_RE` validation, CORS allow-list + CSRF
  origin guard (already applies to POST), size caps, `logging.getLogger("mika.chat")`, `HTTPException`.
- **The chat call gets NO tool/file access** (`claude -p` with **no `--add-dir`**) — it physically
  cannot read other jobs or the filesystem. Scope is enforced at the process level, not just the prompt.

---

## Non-goals (v1)
- No diagnosis, treatment advice, prognosis, or dosing. Explains what the read *says*; defers to the doctor.
- No general medical Q&A. On-study only; off-topic → short redirect.
- No image-grounded vision answers (phase 4). v1 answers from the **report text** only.
- No streaming (phase 3). v1 is request/response with a "reading your report…" state.

---

## Architecture / data flow
```
Read screen ──"Ask"──▶ ChatDrawer (overlay, right slide-over; proof column underneath untouched)
   │  POST /api/chat/{job_id}  { question, history }
   ▼
app.py: chat endpoint  ──▶ services/case_chat.py
   ├─ validate job_id (JOB_ID_RE) + flag + caps
   ├─ load report:  _load_report(job_id)  (durable, BOM-tolerant; survives restart)
   ├─ assemble context: study meta + bottom line + findings(+certainty) + key points + confidence
   ├─ build guardrail system prompt (scope + concise + no-advice + anti-fabrication, mirrors BASE_RULES)
   ├─ ONE-SHOT claude:  [claude_bin,"-p","--output-format","json","--model",M,"--effort","low"]  (NO --add-dir)
   │     prompt on stdin → parse envelope["result"], envelope["is_error"]
   ├─ persist turn → {job_dir}/chat.json  (multi-turn, capped)
   └─ return { answer }   (plain text; frontend derives finding citations client-side)
```

---

## Backend

### New file: `backend/services/case_chat.py`
Self-contained; depends only on stdlib + reuses the auth pattern from `agent_runner`.

**Claude seam (verified, `agent_runner.py:1236-1295`).** No reusable one-shot exists, so add a thin one:
```python
# resolve binary the same way the agent does
from services.agent_runner import _resolve_claude_bin   # (or copy the 3-line resolver to avoid import weight)

def _chat_env() -> dict:                       # mirrors AgentRunner._child_env host-fallback (agent_runner.py:703-706)
    env = dict(os.environ)
    if not os.environ.get("MIKA_AGENT_USE_API_KEY"):          # default desktop posture = host subscription login
        env.pop("ANTHROPIC_API_KEY", None); env.pop("ANTHROPIC_AUTH_TOKEN", None)
    return env
    # v1 SCOPE DECISION (explicit, not accidental): chat uses the host's configured auth — subscription by
    # default, same as the read's fallback branch, and respects MIKA_AGENT_USE_API_KEY. A per-user API-key /
    # setup-token override (AgentRunner._child_env:695-702) is intentionally OUT of v1; the endpoint takes no
    # credential. If a user runs MIKA on an API key, both read and chat then share that key — consistent.

def ask_claude(prompt: str, *, model: str, effort: str, timeout_s: int) -> tuple[str, bool]:
    """One-shot, NO tools/--add-dir. Returns (text, is_error). Never raises."""
    binp = _resolve_claude_bin()
    if not binp:
        return ("", True)
    # SCOPE INVARIANT (load-bearing security): NO --add-dir AND NO --permission-mode. Headless `claude -p`
    # without bypassPermissions cannot auto-approve file tools → zero filesystem reach even from the server cwd.
    # NEVER copy `--permission-mode bypassPermissions` from agent_runner for "parity" — both omissions are required.
    cmd = [binp, "-p", "--output-format", "json", "--model", model, "--effort", effort]
    assert "--add-dir" not in cmd and "--permission-mode" not in cmd, "chat must have no tool/file scope"
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=_chat_env(), timeout=timeout_s)
        env = json.loads(proc.stdout.strip()) if proc.stdout.strip() else {}
        return (env.get("result", "") or "", bool(env.get("is_error")) or proc.returncode != 0)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return ("", True)
```

**Context assembly** — pure, unit-testable; reads only the patient-facing report (de-identified, plain):
```python
def build_context(report: dict) -> str:
    s = report.get("study", {})
    # `report` is the NORMALIZED payload (see endpoint) → patient block is backfilled from agent.summary.patient.
    # Belt-and-suspenders fallback chain so a normal-study / lite report still grounds:
    p = report.get("patient") or {}
    agp = (((report.get("agent") or {}).get("summary") or {}).get("patient")) or {}
    findings = p.get("findings") or agp.get("findings") or []
    bottom = p.get("bottom_line") or agp.get("bottom_line") or ""
    kpts = p.get("key_points") or agp.get("key_points") or []
    lines = [f"STUDY: {s.get('body_part','?')} · {s.get('modality','?')} · calibration: {s.get('calibration_status','?')}",
             f"BOTTOM LINE: {bottom}"]
    for i, f in enumerate(findings, 1):
        lines.append(f"FINDING {i} [{f.get('certainty','')}]: {f.get('plain','')}"
                     + (f" — {f.get('caption')}" if f.get("caption") else ""))
    if kpts: lines.append("KEY POINTS: " + " | ".join(kpts))
    # the report's OWN patient-facing meaning + signposting — so "what it means / next steps" is grounded
    # in the report, not invented from general knowledge:
    wim = p.get("what_it_means") or agp.get("what_it_means") or []
    if wim: lines.append("WHAT IT MEANS (plain, from the report): " + " | ".join(wim))
    wf = p.get("worth_flagging") or agp.get("worth_flagging") or []
    if wf: lines.append("WATCH-FOR / WORTH FLAGGING (from the report): " + " | ".join(wf))
    c = (p.get("confidence") or agp.get("confidence") or {}); lines.append(f"OVERALL CONFIDENCE: {c.get('label','')} ({c.get('score','')}%) {c.get('note','')}")
    return "\n".join(lines)
```
> Uses `patient.*` (layperson) by default; falls back through `agent.summary.patient` so it isn't empty on
> studies whose top-level block wasn't backfilled. A `clinician=True` toggle (swap in `clinician.findings`)
> is deferred to phase 2; v1 = patient view to match the default Read view.

**System prompt (verbatim — mirrors `base_prompt.py` BASE_RULES + REPORT_DISCLAIMER):**
```
You are MIKA, answering a patient's questions about THIS ONE imaging study, using ONLY the report below.

REPORT
<context>

RULES
1. Use ONLY the report above. Never add a finding, measurement, or fact that is not in it. If the answer
   is not in the report, say: "I can't tell that from this read."
2. This study only — but HELP FIRST, don't brush off. A question about *their* reading (incl. "is it
   serious?", "is this cancer?", "should I worry?") deserves a plain answer of what the report DOES and does
   NOT say, then defer the diagnosis itself to their doctor — never a cold "ask your doctor." The short
   redirect is ONLY for genuinely off-study things (other conditions, general medical questions, dosages).
3. Write for a worried non-technical person. Short everyday words, no jargon; if a medical term must appear,
   give its plain meaning in a few words. Be clear and reassuring without overstating.
4. You MAY explain, in plain language, what a finding means for them and how it might affect everyday life
   (as a possibility, not a certainty), and you MAY surface the report's own next-step pointers and any
   "watch-for" symptoms. NEVER recommend a specific treatment, medication, dose, or procedure, and never
   diagnose — their doctor decides those.
5. Match the report's certainty word exactly (Confirmed / Likely / Possible); never sound more sure than it.
   If the study is uncalibrated, never state a millimetre value — use the report's qualitative wording.
6. Be concise: a few short sentences, or up to three short points. When it fits the question, shape the
   answer as — what it is (plain) → what it could mean for you → a plain next step. No preamble.
7. End any answer about what to do with "Discuss this with your doctor"; if the report lists red-flag
   symptoms, name them as reasons to seek care sooner.
```
Then append prior turns as `Q:/A:` pairs and the new `Q:`.

**Deterministic backstops (the answer's LAST writer — not prompt-only).** Per `INCIDENTS.md` ("a safety gate
that looks applied but never fires" + "LLM-stated mm fabricated without calibration"), the system prompt is
*one* line of defense; these run AFTER generation and cannot be talked around:
```python
import re
def answer_case_question(job_id, report, question, history, *, model, effort, timeout_s, data_dir):
    ctx = build_context(report)
    text, err = ask_claude(build_prompt(ctx, history, question), model=model, effort=effort, timeout_s=timeout_s)
    if err or not text.strip():
        return ("", True)
    # BACKSTOP 1 — uncalibrated studies must NEVER state a millimetre value (INCIDENTS.md fabricated-mm class).
    # STRIP the offending mm token(s), don't discard the whole answer — keeps a useful plain reply while
    # guaranteeing no fabricated measurement survives. (Nuking the answer would destroy a correct, helpful
    # reply over an incidental "5 mm slice thickness" — a usefulness regression against the feature's purpose.)
    cal = (report.get("study", {}) or {}).get("calibration_status", "") or ""
    if "uncalibrat" in cal.lower() or cal.strip().upper().startswith("UNCALIBRATED"):
        mm_re = re.compile(r"\b\d+(?:\.\d+)?\s*(?:mm|millimet(?:er|re)s?)\b", re.I)  # 4mm/4 mm/4.5 mm/4 millimetres
        if mm_re.search(text):
            text = mm_re.sub("a size that can't be measured exactly on this uncalibrated study", text).strip()
            if "doctor" not in text.lower():
                text += " Discuss exact measurements with your doctor."
    _persist_turn(data_dir, job_id, question, text)
    return (text, False)
```
> The mm-strip is deterministic and model-independent — exactly the "last writer" discipline INCIDENTS.md
> demands, converting the highest-severity recurring class from prompt-only to *enforced*. It survives a model
> swap, a jailbreak, or pattern-completion arithmetic. **Empty-context is handled upstream:** the endpoint
> 404s a report with no patient block, and `build_context` falls back to `agent.summary.patient`, so the model
> always grounds on real findings (or at least the bottom line + confidence) rather than free-associating.

**Persistence** — `{_job_dir(job_id)}/chat.json` = `[{role, text, ts}]`, capped at `CHAT_MAX_TURNS*2`
(default 24). Best-effort (failure logged, never fatal — same discipline as `_persist_report`).

### New endpoint in `app.py` (place beside the report endpoints, ~line 2310)
```python
CHAT_ENABLED = os.environ.get("MIKA_CHAT_ENABLED", "0") == "1"
CHAT_MODEL   = os.environ.get("MIKA_CHAT_MODEL", os.environ.get("MIKA_AGENT_MODEL", "opus"))
CHAT_EFFORT  = os.environ.get("MIKA_CHAT_EFFORT", "low")
CHAT_TIMEOUT = int(os.environ.get("MIKA_CHAT_TIMEOUT_S", "90"))
CHAT_MAX_Q   = int(os.environ.get("MIKA_CHAT_MAX_QUESTION_CHARS", "1000"))
CHAT_MAX_TURNS = int(os.environ.get("MIKA_CHAT_MAX_TURNS", "12"))   # history pairs kept for re-grounding

class ChatRequest(BaseModel):
    question: str
    history: list[dict] = []           # [{role:"user"|"assistant", text:str}], capped server-side

class ChatResponse(BaseModel):
    answer: str

@app.post("/api/chat/{job_id}", response_model=ChatResponse)
async def case_chat(job_id: str, body: ChatRequest):
    if not CHAT_ENABLED:                raise HTTPException(404, "Not found")
    _validate_job_id(job_id)            # JOB_ID_RE — anti-traversal
    q = (body.question or "").strip()
    if not q:                           raise HTTPException(400, "Empty question")
    if len(q) > CHAT_MAX_Q:             raise HTTPException(413, "Question too long")
    # Load the SAME normalized payload the UI renders. Raw report.json has an EMPTY top-level patient block
    # on most studies (the real findings live under agent.summary.patient; _normalize_loaded_report backfills
    # it — that's why the screen shows findings). Sourcing raw _load_report here would starve the chat of
    # grounding on ~3/4 of real studies, breaking the "grounded in your report" safety claim.
    if job_id in JOBS and JOBS[job_id].status == "complete":
        report = _build_report_payload(JOBS[job_id])
    else:
        raw = _load_report(job_id)
        report = _normalize_loaded_report(job_id, raw) if raw else None
    if not report or not report.get("patient"):  raise HTTPException(404, "Report not found")
    from services.case_chat import answer_case_question
    text, err = answer_case_question(job_id, report, q, body.history[-CHAT_MAX_TURNS:],
                                     model=CHAT_MODEL, effort=CHAT_EFFORT, timeout_s=CHAT_TIMEOUT,
                                     data_dir=DATA_DIR)
    if err:                             raise HTTPException(503, "Chat is unavailable right now")
    return ChatResponse(answer=text)

@app.get("/api/chat/availability")   # lets the UI hide the trigger when the flag is off (mirrors /api/agent/availability:1894)
async def chat_availability():
    return {"enabled": CHAT_ENABLED}
```
- **Run off the event loop:** wrap `answer_case_question` in `await asyncio.to_thread(...)` (subprocess is blocking).
- **CSRF:** POST → existing origin-guard middleware (`app.py:209-231`) already enforces same-origin. No new work.
- **No server rate guard.** Loopback single-user (127.0.0.1 / Electron) has no concurrent abuser; the UI
  disables send while a request is in flight. (An earlier draft's per-job 429 was over-engineering for this
  deployment model — cut. `JOB_ID_RE` + CSRF + the char cap are the right, free defenses to keep.)

---

## Frontend (`frontend/index.html`, additive)

> **Drawer UI/UX = the "Beside" concept — build to [docs/Mika_Chat_Drawer_Concept.md](Mika_Chat_Drawer_Concept.md)**
> (forged in /daedalus, approved). Key refinements over the bare spec below: the reply is an **anchored answer
> note** (certainty glyph + "about your <certainty> finding · <level>" + a `view on scan` chip), NOT a chat
> bubble; the signature interaction is **"peek" — `view on scan` DIMS the drawer (doesn't close it)** and lights
> the body marker + swaps the proof underneath, then re-focus to continue; the thinking state is a **slow
> breathing pulse on the MIKA mark** (no bouncing dots); motion = slide from the inline-end 240ms + note
> fade/rise 200ms, `prefers-reduced-motion` → fades only; RTL flips the drawer to the inline-start (glyph +
> figure NOT mirrored). MIKA's existing tokens only (single accent #2563EB) — no new color/gradient/glass.

1. **State (App, beside `showAbout`/`showConnect`, ~line 2513):**
   `const [chatOpen,setChatOpen]=useState(false); const [chatMsgs,setChatMsgs]=useState([]);`
2. **Trigger** — in `TopBar` read branch (`topbar-r`, before Download — re-grep anchor), render **only if**
   chat is enabled: fetch `GET /api/chat/availability` once on mount → `{enabled}`, gate the button on it, so a
   default `MIKA_CHAT_ENABLED=0` build shows NO button (acceptance criterion 1 — zero flag-off change):
   `<button className="btn btn-sm" onClick={()=>setChatOpen(true)}>{I.message(...)} Ask about your read</button>`
3. **`api.chat`** (beside the other helpers, ~line 1025):
   `chat:(id,question,history)=>fetch(`${API_BASE}/api/chat/${id}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question,history})}).then(r=>r.json())`
4. **`ChatDrawer` component** — reuse the `.overlay` backdrop (`index.html:666`) + a NEW `.chat-drawer`
   that anchors **right** instead of centered (so it slides over the proof column, not the whole screen):
   ```css
   .chat-drawer { position:absolute; top:0; right:0; bottom:0; width:380px; max-width:92vw;
     background:var(--surface); border-left:1px solid var(--line); display:flex; flex-direction:column;
     animation: drawerIn var(--duration-normal) var(--ease-out); }
   @keyframes drawerIn { from{transform:translateX(16px);opacity:0;} to{transform:none;opacity:1;} }
   .chat-thread { flex:1; min-height:0; overflow:auto; }   /* only the THREAD scrolls — page rule intact */
   @media (max-width:760px){ .chat-drawer{ width:100%; } }  /* full-screen sheet on mobile */
   ```
   The overlay (`position:fixed; inset:0`) hosts it; clicking the dimmed area or ✕ closes. The Read grid
   underneath is unchanged and does not scroll.
5. **States:** empty (3 suggested chips + the "this study only" pill), thinking ("MIKA is reading your
   report…"), answer bubbles, error ("Couldn't answer just now — try again"), refusal renders as a normal
   short answer (the model self-refuses; no special UI).
   - **Suggested chips (the purpose, made discoverable — plain questions about understanding their own read):**
     1. "What does my result mean in simple terms?"
     2. "Is this something I should worry about?"
     3. "What should I ask my doctor next?"
     These teach a worried patient what the chat is for and seed the meaning → impact → next-step shape.
6. **Grounding (client-side, robust — no model-JSON dependency):** after an answer renders, scan it for a
   finding reference using the **existing** `parseLandmark(anatomy, answerText)` / finding numbers; if it
   matches a finding in `vm.findings`, show a `view on scan` chip → `selectFinding(id)` (existing,
   `index.html:2376`) which highlights the body marker + swaps the proof image, and close the drawer so the
   proof is visible. Re-open to continue. Reuses `selectFinding` + `ProofPanel` + `landmarkXY` verbatim.

---

## API contract
`POST /api/chat/{job_id}` · body `{ "question": str, "history": [{role,text}] }` · `200 {"answer": str}`
· `400` empty · `413` too long · `404` flag off / bad id / no report · `503` model unavailable.

---

## Security review (each vector)
| Vector | Mitigation |
|---|---|
| Path traversal via job_id | `_validate_job_id` (`^[0-9a-f]{8}$`) before any disk touch |
| Reading other jobs / fs | chat `claude -p` runs with **no `--add-dir`** → no tool/file access; context is only this job's report |
| Cross-origin data theft | CORS allow-list `allow_credentials=False` + CSRF origin guard on POST (existing) |
| Prompt-injection in the question | system prompt is authoritative + report is the only ground truth; no tools to abuse; output is plain text |
| Oversized / abusive input | question char cap (413), history capped server-side (no rate guard — single-user loopback) |
| Off-by-default risk | `MIKA_CHAT_ENABLED=0` → endpoint 404s, button hidden |
| PHI leakage | answers derive only from the user's own report; nothing new exposed; loopback 127.0.0.1 posture unchanged |

---

## Edge cases & failure modes
| Case | Behavior |
|---|---|
| `claude` not logged in / missing | `ask_claude` → `is_error=True` → 503 → UI "unavailable, try again" |
| Timeout (90s) | caught → 503 |
| Report has no findings (normal study) | context still has bottom line + confidence; model answers from those |
| Uncalibrated study | prompt rule 4 forbids mm; model mirrors qualitative wording |
| Question in another language | model answers in kind (Claude handles), still scoped |
| Multi-turn drift | history capped + system prompt re-asserts scope each call (stateless re-grounding) |
| Concurrent questions same job | UI disables send while a request is in flight (single-user; no server guard) |
| Malformed history from client | server validates/coerces shape, drops bad entries |

---

## Testing (`tests/test_case_chat.py`, mirrors `tests/test_run1_contract.py`)
**Unit (pure, deterministic):**
- `build_context` → contains study line + each finding + certainty + confidence; **and a regression that on a
  report whose top-level `patient` is empty but `agent.summary.patient.findings` is populated, the context
  still lists the findings** (guards the Gap-1 normalized-load fix on the real on-disk shape).
- system-prompt builder → contains scope rules + the report + history; under a token budget.
- **mm backstop (token-strip)** → uncalibrated report + an answer "...about 4 mm..." → the `\d mm` token is
  REMOVED (assert no `\d+\s*(mm|millimet)` survives) **and the rest of the answer is PRESERVED** (not replaced
  wholesale — the helpful prose around it stays); calibrated report → mm passes through untouched.
- **scope invariant** → the chat command list built by `ask_claude` contains **neither `--add-dir` nor
  `--permission-mode`** (locks the no-filesystem guarantee against future drift).
- response handling → trims, never returns tool/markup; empty model output → treated as error.

**Endpoint (claude mocked):** monkeypatch `case_chat.ask_claude` to return a canned answer.
- flag off → 404; bad job_id → 404; no report → 404; empty Q → 400; 1001-char Q → 413; happy path → 200 + answer.
- `GET /api/chat/availability` → `{enabled:false}` when flag off, `{enabled:true}` when on (gates the UI button).
- persistence: `chat.json` written, reloaded, capped.
- isolated `DATA_DIR` fixture + `_make_completed_job` pattern (existing).

**Cannot be unit-verified (name it):** the **live** `claude -p` call — a headless `claude` spawned *inside a
Claude session deadlocks* (`docs/INCIDENTS.md`). So end-to-end is a **human gate**: enable the flag, open a
real read in the app, ask — a plain-meaning question, a "how will this affect my daily life", a "what do I do
next", plus adversarial ("is it cancer", "what medication/dose should I take", off-topic, a question whose
answer isn't in the report). Confirm: plain non-technical wording (meaning → life impact → next step),
concision, on-study scope, certainty match, and — the blocker — that "what to do" stays **non-prescriptive**
(surfaces the report's own pointers + "discuss with your doctor", never "take X"). No CI/agent run substitutes.

---

## Phased rollout
- **P1 — text Q&A (this plan), behind the flag.** Endpoint + `case_chat.py` + drawer + 3 states. Ship dark.
- **P2 — grounding chips.** Client-side finding citation → `selectFinding`. (Small; data already on screen.)
- **P3 — streaming.** `--output-format stream-json` + SSE, token-by-token. Optional polish.
- **P4 — image-grounded.** Pass the active finding's proof image; gate behind the same calibration/region
  trust rules (`trustAllowsProof`). Higher cost + hallucination surface — only after P1–P3 are stable.

## Risks & mitigations
- **Scope creep into advice** (top risk) → prompt rules 2–3 + the "discuss with your doctor" close + refusal
  testing. If a single answer gives advice in testing, treat as a release blocker.
- **Latency** (`claude -p` cold start) → `effort=low`, 90s cap, "reading your report…" state; consider a
  smaller `MIKA_CHAT_MODEL` if opus feels slow (sonnet is fine for grounded Q&A).
- **Inconsistent certainty vs report** → prompt rule 4 + the model only sees the report's certainty words.

## Acceptance criteria ("done")
1. Flag off = literally zero behavior change (button hidden, endpoint 404s, suite green).
2. Flag on, real read: answers are plain/non-technical (what it is → what it could mean for daily life → a
   plain next step), concise, on-study, mirror the report's certainty, and — the release blocker — NEVER
   prescribe a specific treatment, medication, dose, or procedure; "how to move forward" surfaces the report's
   own pointers + "discuss with your doctor". Verified in the human run incl. the adversarial prompts.
3. No edits to the existing read/agent pipeline; new tests green; drawer introduces no page scroll.
