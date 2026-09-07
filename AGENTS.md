# AGENTS.md — Tool-neutral operating contract

This is the same contract as `CLAUDE.md`, for any coding agent (Codex, Cursor, Aider,
Copilot Workspace, an in-repo agent, or a human). `CLAUDE.md` is the authoritative copy
of *this instructions contract*; if the two ever diverge, `CLAUDE.md` wins and this file
must be updated in the same PR.

This file governs how an agent works. It does not define what the project is — that is
`docs/PROJECT.md` (§1–§35), an independent, frozen knowledge canon that this contract
answers to on project facts but does not itself own. See `CLAUDE.md` §0.

## Canon
`docs/PROJECT.md` (§1–§35) is frozen and authoritative. Cite it as `[P§n]` in every
normative statement. Never edit or summarize it. Gaps go to
`docs/state/OPEN_QUESTIONS.md` or an ADR — never silent invention, never silent omission.

## Read order at session start
`docs/state/STATUS.md` → `docs/state/DECISIONS.md` → the relevant ADRs → the issue.

## Hard rules
1. Problem first — characterize before selecting `[P§29.1]`.
2. Baseline first — measure before optimizing `[P§29.2]`.
3. Evidence over hype — quantitative comparison or no claim `[P§29.3]`.
4. Hardware-aware — accuracy without deployability is incomplete `[P§29.4]`.
5. Reproducibility — full experiment metadata or the result is void `[P§25]`.
6. Boundary test — every module answers "what does this own, and why not elsewhere?" `[P§34]`.
7. No layer leakage between reasoning / orchestration / knowledge / execution `[P§19]`–`[P§22]`.
8. No provider specifics outside the LLM gateway `[P§20]`.
9. Don't reinvent existing NVIDIA/CUDA/vendor expertise — discover and invoke `[P§15]`.
10. Human approval for expensive, destructive, irreversible, or production-affecting
    actions, per `docs/APPROVALS.md` `[P§24]`.
11. Research and cite with dates when knowledge may be stale, weighted per
    `docs/RESEARCH_POLICY.md` `[P§17]`.
12. Prefer deleting a subsystem to bolting one on `[P§34]`.

## Gates
- No implementation code without an issue.
- No architectural code without an ADR.
- Architect sessions produce ADR + interface stubs.

## Workflow
The project uses a single trunk: `main` + short-lived work branches. The project owner
is the Official PM who reviews and approves all changes before merging to `main`.
Work is performed on one short-lived `feature/<owner>/<work>` (or `fix/`/`docs/`/`ci/`/`hotfix/`)
branch cut from `main`; technical review by the Official PM and passing CI are required
before merging to `main`. Never commit directly to `main` (except rolling-state acceptance
syncs authorized by the Official PM). The `dev-munna` trunk is permanently retired.

## Rolling state (before every commit)
Rewrite `STATUS.md` (≤60 lines, present tense). Append to `JOURNAL.md`, `DECISIONS.md`,
`EXPERIMENTS.md`. Update `OPEN_QUESTIONS.md` by strikethrough, not deletion.

## What this is NOT `[P§31]`
Not a chatbot, not a YOLO wrapper, not NVIDIA-only, not unlimited autonomy, not a RAG
dump, not a fixed technique list, not a prompt pile.
