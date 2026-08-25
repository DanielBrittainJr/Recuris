# Third-party components

Nothing third-party is redistributed in this repository. `third_party/` holds a
pinned upstream reference, our patch, and a setup script; the components
themselves are fetched into `external/`, which is git-ignored. This section
records what those components are, under what terms, and exactly what we change.

## tau2-Bench

- Upstream: <https://github.com/sierra-research/tau2-bench>
- Licence: MIT, © 2025 Sierra Research
- Pinned: harness `8ebb749`, domain data tag `v1.0.1` (`fc0055dc`)
- Our changes: `third_party/tau2/recuris.patch`, eleven files under `src/tau2/`

The changes are three groups: orchestrator hooks for the terminal
working-memory gate and the status board (all env-gated, so an unset run is
upstream behaviour); the NL-assertion judge and environment-interface model
constants; and an OpenAI-compatible embedder for the knowledge-base domains.
`third_party/tau2/README.md` describes each, and the README records the
consequence of the judge change for cross-paper comparison.

The MIT licence permits redistribution with its notice attached. We use the
patch-and-fetch form anyway, because the configuration we evaluated is v1.0.1
data on v1.0.0-lineage code, and vendoring it would freeze that hybrid into an
opaque blob nobody could inspect.

## SkillFlow

- Upstream: <https://github.com/ZhangZi-a/SkillFlow>
- Licence: **none stated**
- Pinned: `7b49ff5`
- Task set: `zhang-ziao/SkillFlow-Task` (Hugging Face)
- Our changes: `third_party/skillflow/recuris.patch`, two files

With no licence, all rights are reserved and redistribution is not permitted.
Not one line of SkillFlow appears in this repository. `setup.sh` clones the
upstream repository and applies our patch to the user's own copy.

The substantive change is a template-injection fix: `NoInstallQwenCode`
overrides `run()`, which bypasses the harbor base class's
`prompt_template_path` rendering hook, so a skill arm configured with a
template silently ran as a bare baseline. The second change is harbor API
compatibility in the family job runner.

## Harbor

- Upstream: <https://github.com/harbor-framework/harbor>
- Licence: Apache-2.0
- Version: 0.20.0
- Our changes: `third_party/harbor/harbor-0.20.0-docker.patch`, two files under
  `src/harbor/environments/docker/`

**Stated changes, per Apache-2.0 §4:** `docker.py` keeps the exec stdin pipe
open for the lifetime of a `compose exec` call and passes `-T` so no TTY is
allocated; `docker_unix.py` adds a `HARBOR_DOCKER_UPLOAD_MODE=tar` path that
skips `docker compose cp`. Both address host-specific container behaviour and
are conditional — on a normal Docker host, stock harbor works and the patch
should not be applied. Harbor is installed as an ordinary dependency; we do not
redistribute it.

## Terminal-Bench 2.1

- Upstream: <https://github.com/harbor-framework/terminal-bench-2-1>
- Licence: Apache-2.0
- Pinned: `5c8eadf`, 91 tasks
- Our changes: none

The benchmark runs unmodified. Our agent attaches through harbor's
`import_path` mechanism, so the task set never has to change. The task
container images belong to their authors and are not redistributed; see
`third_party/tb21/README.md` for why the image snapshot is a hard precondition
rather than a detail.

## Coding agent for the evolution loop

The recursive evolution loop calls an external agentic coding CLI for every
generative step. That software is proprietary and is not distributed here. We
ship the launcher contract and a reference launcher for Claude Code; see
`src/recuris/metaagent/launchers/README.md`.
