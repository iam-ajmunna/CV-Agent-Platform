# CV Engineering Agent — GitHub Development & Delivery Flow

**Status:** Approved Working Process
**Version:** V1.3
**Repository:** `iam-ajmunna/CV-Agent-Platform`

> Current project flow: `main` + short-lived branches. The project owner is the
> Official PM who reviews and approves all changes before merging into `main`.
> The `dev-munna` intermediate trunk is permanently retired.

## 1. Purpose

This document defines how CV Engineering Agent is developed, reviewed, tested, and
merged through GitHub.

The project follows:

- protected `main` as the official integration and release baseline;
- short-lived work branches cut directly from `main` for each issue/work item;
- mandatory automated CI/CD validation;
- Official PM review and approval before merge into `main`;
- small, isolated feature increments.

## 2. Branching Model

```text
                         main
                          ▲
                          │ Official PM review + CI
                          │
             short-lived work branch
```

### Rules

1. `main` receives no direct unreviewed development commits or pushes.
2. Short-lived branches are cut from `main`.
3. PRs from short-lived branches target `main`.
4. Passing CI and Official PM review/approval are required before merging a work branch into `main`.
5. Short-lived branches are deleted after merge.
6. Never force-push `main`.
7. The `dev-munna` branch is permanently retired.

## 3. One Work Item at a Time

```text
Issue
  ↓
short-lived branch from main
  ↓
implementation + tests
  ↓
PR → main
  ↓
CI + Official PM review & merge
  ↓
main
```

Keep diffs reviewable; if a change is too large, split the issue.

## 4. Branch Naming

```text
feature/<owner>/<work>
fix/<owner>/<work>
docs/<owner>/<work>
ci/<owner>/<work>
hotfix/<owner>/<work>
```

Branches should normally live less than three days. Longer-lived work requires an
explicit project-management reason.

## 5. Pull Request Requirements

Every PR must contain:

- problem statement;
- scope;
- implementation summary;
- architecture impact;
- tests performed;
- acceptance criteria;
- risks and dependencies;
- documentation impact where applicable.

## 6. CI/CD Gate

Every PR must pass automated validation before merge.

```text
Formatting → Linting → Type Checking → Unit Tests → Integration Tests
→ Security Checks → Package Build → Architecture / Spec Checks
```

GPU-specific validation is added when GPU infrastructure is available. CPU CI must
remain capable of validating the core architecture without GPU infrastructure.

## 7. Local Validation

Before opening a PR, run the local equivalent of configured formatting, linting,
type-checking, tests, and build checks. The PR should not be the first place an obvious
failure is discovered.

## 8. Review and Merge

### Official PM review & merge

The Official PM reviews work-branch PRs for correctness, architecture consistency, tests,
security, performance implications, API compatibility, and documentation, and performs the
merge into `main`.

### Merge strategy

Prefer squash merging short-lived branches so `main` retains meaningful history.

## 9. Keeping Branches Current

Before opening or merging a PR, synchronize the work branch with the current
`main` state. Never force-push `main`.

## 10. Work Completion

```text
[ ] Issue identified
[ ] Acceptance criteria defined
[ ] Implementation complete
[ ] Tests complete
[ ] Documentation updated
[ ] Local validation passes
[ ] CI passes
[ ] Official PM review and approval complete
[ ] PR merged to main
[ ] Post-merge main CI passes
[ ] Short-lived branch deleted
```

## 11. Workstreams

| Workstream | Branch prefix | Owns |
|---|---|---|
| Architecture & Documentation | `docs/*` | ADRs, architecture, lifecycle, contracts, governance |
| Core Runtime | `feature/*/runtime-*` | LangGraph, state, sessions, checkpoints, interrupts |
| LLM Gateway | `feature/*/llm-*`, `feature/*/provider-*` | provider abstraction, routing, structured output, streaming, fallbacks |
| Knowledge / RAG | `feature/*/knowledge-*`, `feature/*/rag-*` | retrieval, provenance, freshness |
| Research Engine | `feature/*/research-*` | web/GitHub/paper research and evidence verification |
| MCP / Tools | `feature/*/mcp-*`, `feature/*/tool-*` | typed execution boundaries |
| CV Intelligence | `feature/*/requirement-*`, `feature/*/*-agent-*` | CV reasoning layer |
| Training / Experimentation | `feature/*/experiment-*`, `feature/*/training-*` | experiments, datasets, training launchers |
| Evaluation / Benchmarking | `feature/*/evaluation-*`, `feature/*/benchmark-*` | metrics, benchmarks, failure analysis |
| NVIDIA / Edge | `feature/*/nvidia-*`, `feature/*/tensorrt-*`, `feature/*/deepstream-*`, `feature/*/jetson-*` | CUDA, TensorRT, DeepStream, Jetson |
| Deployment | `feature/*/deployment-*`, `feature/*/monitoring-*` | packaging, deployment, rollback, monitoring |

## 12. Non-Negotiable Rules

```text
1. No direct development commits to main without PR or PM authorization.
2. Short-lived work branches start from main.
3. Every work PR requires CI and Official PM review.
4. The dev-munna trunk is permanently retired.
5. No force-push to main.
6. Expensive/destructive CV-agent operations remain subject to docs/APPROVALS.md.
7. Documentation and tests are part of feature completion.
```

## 13. Current Operating State

```text
Integration baseline: main
Work branches: short-lived branches cut from main
Review & approval: Official PM
Current milestone: Phase 1 — Architecture & ADR Specification
```
