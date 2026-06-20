# Final Prompt for Run 1 Implementation

Use this prompt in a fresh implementation session.

```text
You are implementing Run 1 from the locked MIKA hybrid architecture plan.

Read first:
C:\Users\husam\OneDrive\Documents\MRI_Analayis_AI\docs\MIKA_LOCKED_ARCHITECTURE_PLAN.md

Goal:
Make the current MIKA app stable and usable for nontechnical users before adding the evidence/CV architecture.

Scope for this run:
1. Stabilize the completed-report contract.
   - Normalize agent outputs into stable report fields used by API, UI, PDFs, and persistence.
   - Ensure agent jobs do not leave the main interpretation empty when `agent.summary` exists.
   - Ensure recent studies have stable title, modality, anatomy, PDF availability, and status.

2. Fix patient and clinical PDF plumbing.
   - Make patient PDF and clinical PDF availability reliable for live and persisted jobs.
   - Fix `/api/report/{job_id}`, `/api/reports`, `/api/reports/{job_id}/pdf`, and `/api/reports/{job_id}/clinical-pdf` behavior as needed.
   - Completed reports must reload after server restart.

3. Fix Claude auth and retry state for nontechnical users.
   - Keep the default path on the user's Claude subscription via Claude CLI.
   - Do not require users to open a terminal.
   - Replace or extend `/api/connect` so the app can manage an auth session:
     browser sign-in, polling, retry, cancel, and pasted-code fallback if the CLI asks for a code.
   - Add clean auth states and plain-language errors.
   - Clear stale auth errors when a retry or rerun succeeds.
   - Add a cheap readiness/preflight check separate from full `opus/high` analysis.

4. Add focused tests.
   - Report contract normalization.
   - Patient and clinical PDF route availability.
   - Persisted report reload after restart.
   - Auth state cleanup after failed then successful run.
   - Claude CLI missing / not signed in / signed in states.

Constraints:
- Do not implement StudyGraph, EvidencePack, ArtifactRegistry, ArtifactQaGate, CV measurements, verifier logic, or UI redesign in this run.
- Do not copy medical files or private reports into the repo.
- Do not commit generated report outputs.
- Do not package the EXE yet.
- Do not switch the normal path to Anthropic API-key billing.
- Preserve unrelated existing worktree changes.

Expected deliverables:
- Code changes for Run 1 only.
- Tests for the changed behavior.
- A short verification summary with exact commands run.
- A git commit containing only Run 1 implementation changes.

Implementation preference:
Make the smallest architecture change that creates a stable contract and reliable auth/report plumbing. Keep compatibility with existing saved reports by supporting old fields as fallbacks, but write the new normalized shape going forward.
```
