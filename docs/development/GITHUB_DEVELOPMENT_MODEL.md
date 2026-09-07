# CV Engineering Agent — GitHub Development Model

**Audience:** Project owner, developers, AI/CV coding agents
**Status:** Approved operating model
**Repository:** `iam-ajmunna/CV-Agent-Platform`

> The project operates on a single-trunk development flow: `main` + short-lived
> work branches. The project owner is the Official PM who reviews and approves
> all changes before merging into `main`. The `dev-munna` intermediate trunk is
> permanently retired.

## 1. Executive Summary

- **`main`** is the official integration and release baseline.
- Work is performed on short-lived branches cut directly from `main`.
- Developers and coding agents never commit directly to `main` (except rolling-state acceptance syncs authorized by the Official PM).
- Every short-lived branch requires CI and Official PM review and approval before merge into `main`.
- The `dev-munna` branch is permanently retired.

## 2. Branch Roles

```text
                         main
                          ▲
                          │ Official PM review + CI
                          │
             short-lived feature/fix/docs/ci branch
```

### `main`

- Official integration and release baseline.
- No direct unreviewed development commits.
- Updated exclusively through short-lived branch PRs reviewed and merged by the Official PM, or authorized acceptance synchronization commits.

### Short-lived branches

- Cut from `main`.
- One issue/work item per branch.
- Prefixes: `feature/`, `fix/`, `docs/`, `ci/`, `hotfix/`.
- Deleted after merge into `main`.

## 3. Non-Negotiable Rules

```text
1. main is the single trunk; no direct development commits without PR or explicit PM authorization.
2. The dev-munna trunk is permanently retired and deleted.
3. Short-lived work branches start from main.
4. Every work branch requires CI and Official PM review before merge to main.
5. Never force-push main.
6. Expensive/destructive CV-agent operations remain subject to docs/APPROVALS.md.
```

## 4. Pull Request and CI/CD Requirements

See `docs/development/GITHUB_FLOW_V1.md` for the procedural PR template, CI/CD
requirements, branch naming, and review gates. Both documents must remain consistent.

## 5. Development Sequence

The project is implemented in dependency order, matching the architecture roadmap:

```text
01 Architecture & Specification
02 Runtime Foundation
03 LLM Gateway
04 Knowledge / RAG Foundation
05 Research Engine
06 MCP / Tool Layer
07 Requirement Agent
08 Research Agent
09 CV Architecture Agent
10 Dataset Agent
11 Model Agent
12 Experiment Manager
13 Training Engine
14 Evaluation Engine
15 Benchmark Engine
16 Failure Analysis
17 Optimization Engine
18 NVIDIA / TensorRT / CUDA / DeepStream
19 Deployment Engine
20 Monitoring
21 Full Engineering Loop
```

## 6. Traceability

```text
Requirement
   ↓
GitHub Issue
   ↓
Short-lived Branch (from main)
   ↓
Commit(s)
   ↓
PR → main
   ↓
CI/CD + Official PM Review & Merge
   ↓
main
```

## 7. Completion Definition

```text
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

This is the canonical GitHub development model for the current project.
