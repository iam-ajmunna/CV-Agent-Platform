# CLAUDE.md — Operating instructions for coding agents

**This file is instructions, not knowledge.** The knowledge lives in `docs/`. Keep this
file under 150 lines. If you are tempted to add CV knowledge here, it belongs in
`docs/PROJECT.md` (frozen), an ADR, or `docs/GLOSSARY.md`.

Project: **CV Engineering Agent** — an AI system that *performs* computer-vision
engineering, not one that merely knows computer vision.

---

## 0. Two canons, not one chain

This repo has two independent authorities, scoped to different things:

- **Knowledge canon:** `docs/PROJECT.md` — what the project *is* (§1–§35).
- **Instructions canon:** this file, `CLAUDE.md` — how an agent *works*.
  `AGENTS.md` is a tool-neutral mirror of this file for non-Claude agents; if the
  two diverge, `CLAUDE.md` wins and `AGENTS.md` must be updated in the same PR.

Neither canon defers to the other — `docs/PROJECT.md` does not govern *how* you
work, and `CLAUDE.md` does not define *what the project is*.

## 1. Canon

`docs/PROJECT.md` is the frozen, authoritative project definition, numbered §1–§35.

- **Never edit, reorder, summarize, or "improve" it.** It changes only by an explicit
  human instruction, never as a side effect of another task.
- Every normative statement you write anywhere else must cite its origin as `[P§12]`.
- If the canon is silent on something you need, do **not** invent a requirement. Either
  add it to `docs/state/OPEN_QUESTIONS.md`, or make the call in an ADR that names the
  gap. Silent invention and silent omission are both defects.
- To override the canon, write an ADR that quotes the section it revises and states why.

## 2. Session start — read in this order

1. `docs/state/STATUS.md` — where the project actually is.
2. `docs/state/DECISIONS.md` — what is already settled.
3. Only the ADRs relevant to the task at hand.
4. The GitHub issue you are working.

Do **not** bulk-read `docs/`. If `STATUS.md` contradicts the repository, stop and say so
rather than guessing which is right.

Two more trees exist and are not part of this read order — consult them only when the
task needs them: `spec/*.md` (technical elaborations of specific canon areas — see
README's document map for which spec file covers what; none describe anything
implemented except `10-capability-registry.md`) and `docs/development/` (the git
workflow — read before opening a branch or PR).

## 3. Hard rules — non-negotiable

Derived from `[P§29]`. If a request conflicts with these, say so and stop. Do not comply
quietly and do not work around them.

1. **Problem first.** Never propose a model, architecture, or framework before the
   operational problem is characterized `[P§29.1]`, `[P§5]`.
2. **Baseline first.** Never optimize before a measured baseline exists `[P§29.2]`,
   `[P§11]`. NAS and sweeps are instruments, not defaults.
3. **Evidence over hype.** Never claim an improvement without a quantitative comparison
   against that baseline `[P§29.3]`, `[P§29.10]`. Newer is not better.
4. **Hardware-aware.** Accuracy without deployability is incomplete. Any model
   recommendation states its latency/memory/power implications on the target
   `[P§29.4]`, `[P§12]`, `[P§13]`.
5. **Reproducibility.** No result is valid without the full experiment metadata schema in
   `docs/state/EXPERIMENTS.md` `[P§25]`, `[P§29.5]`.
6. **Boundary test.** No new module, package, or subsystem without answering *what
   responsibility does this own, and why does that responsibility not belong to an
   existing layer?* `[P§34]`. If you cannot answer cleanly, the boundary is wrong — say
   so instead of writing the code.
7. **No layer leakage.** Reasoning, orchestration, knowledge, and execution stay
   separate `[P§19]`, `[P§21]`, `[P§22]`. See `docs/architecture/OVERVIEW.md`.
8. **Provider agnosticism.** No LLM provider name, SDK, or model string outside the LLM
   gateway `[P§20]`.
9. **Don't reinvent.** Discover and invoke existing NVIDIA skills, CUDA agent, TAO,
   TensorRT, DeepStream tooling rather than reimplementing their knowledge `[P§15]`,
   `[P§29.9]`.
10. **Human approval.** Expensive, destructive, irreversible, or production-affecting
    actions require explicit approval per `docs/APPROVALS.md` `[P§24]`, `[P§29.8]`.
    Estimate the cost *before* asking.
11. **Current knowledge.** When a fact may be stale — model releases, benchmarks, library
    behavior — research it and cite the source with a date. Weight sources per
    `docs/RESEARCH_POLICY.md` `[P§16]`, `[P§17]`.
12. **Not a pile of features.** Prefer deleting a subsystem to bolting one on `[P§34]`.

## 4. What this project is NOT `[P§31]`

Not a chatbot. Not a YOLO wrapper. Not NVIDIA-only. Not unlimited autonomy. Not a RAG
dump. Not a fixed technique list. Not hundreds of hardcoded prompts. If a change moves
the system toward any of these, flag it.

## 5. Architecture gate

- **No implementation code without a GitHub issue.**
- **No architectural code without an ADR.** Architectural = introduces a module, changes
  an interface between layers, chooses a dependency, or changes state shape.
- ADRs live in `docs/architecture/adr/`, numbered, from `ADR-0000-template.md`.
- When acting as architect: produce the ADR and **interface stubs only** — types and
  signatures, no bodies. Implementation is a separate session.

## 6. Implementation loop — per issue

1. Read the issue and its governing ADR. No ADR + architectural change ⇒ stop.
2. Plan first: files, interfaces, tests, and explicitly what you will **not** change.
3. Branch `feature/<owner>/<work>` (or `fix/`, `docs/`, `ci/`, `hotfix/` — see
   `docs/development/GITHUB_FLOW_V1.md` §4) from `main`. **Never commit directly to
   `main`.** PRs from short-lived branches target `main`; the Official PM reviews and
   merges them after passing CI.
4. Write the acceptance test first, from the issue's acceptance criteria. Show it
   failing.
5. Implement the minimum that makes it pass. No speculative abstraction. No unrelated
   refactors.
6. Lint, type-check, full test suite.
7. Update rolling state (§7).
8. Commit (conventional commits, body cites issue + ADR), push, open a PR from the
   template.
9. Report what you changed, what you deliberately did not, and what you are unsure about.

Keep diffs reviewable — under ~400 lines. If the issue cannot fit, split the issue.

## 7. Rolling state protocol

Before every commit, and at the end of every session:

| File | Operation | Contents |
|---|---|---|
| `docs/state/STATUS.md` | **Rewrite**, max 60 lines | Current phase, in-flight work, next 3 actions, blockers. Describes **now**, not history. |
| `docs/state/JOURNAL.md` | **Append** one dated entry | What changed, why, what broke, what was learned. Never edit or delete past entries. |
| `docs/state/DECISIONS.md` | **Append** one line per decision | Decision, date, link to ADR. Architectural decision without an ADR ⇒ write the ADR first. |
| `docs/state/OPEN_QUESTIONS.md` | Append new; strike through answered | Keep the strikethrough — do not delete answered questions. |
| `docs/state/EXPERIMENTS.md` | **Append** one row per run | Full `[P§25]` schema. Missing metadata ⇒ not a valid result. |

`STATUS.md` is the only file that shrinks. Everything else only grows.

## 8. Style

- Python, typed, `ruff` + `mypy` clean. Tests with `pytest`.
- Registries hold typed identities, not free strings `[P§32]`.
- Package-safe resource loading; explicit dependencies `[P§32]`.
- Docstrings state the responsibility the unit owns.
- No `TODO` without a corresponding GitHub issue number.

## 9. Commands

```
pip install -e ".[dev]"   # install package + pytest
pytest                    # full test suite (testpaths = tests/)
pytest tests/test_x.py::test_name   # single test
python -m cv_agent         # CLI health check (or: cv-agent, once installed)
```

`ruff` and `mypy` are required clean per §8 but are not yet in `pyproject.toml`
dev-dependencies — installing and running them is part of closing that gap, not a
sign they should be skipped.
