# SkillFlow

Upstream: <https://github.com/ZhangZi-a/SkillFlow> at commit `7b49ff5`.
Task set: `zhang-ziao/SkillFlow-Task` on Hugging Face.

**SkillFlow carries no licence file.** With no licence, all rights are
reserved, and we may not redistribute any part of it — not the agent library,
not a config, not a single line. This directory therefore contains only our own
patch and the reference needed to fetch theirs. `setup.sh` clones their
repository into `external/` and applies the patch to your copy.

If the authors add a licence, this can be revisited. Until then the seam is not
a stylistic preference, it is the only lawful option.

## What `recuris.patch` changes

Two files, and only one of them matters scientifically.

**`libs/harbor_noinstall_agents/agents.py` — the template-injection fix.**
`NoInstallQwenCode` overrides `run()`, which bypasses the harbor installed-agent
base class's `prompt_template_path` rendering hook. The consequence was silent
and expensive: a skill arm configured with a template ignored it and ran as a
bare baseline, producing a "no effect" result that was really a plumbing bug.
The patch renders the template exactly as the base class would, and changes
nothing when no template is configured.

Check this if you change the rendering: a skill config must name a template
file that exists, and the rendered prompt must contain the machine document's
marker. A regression to the silent-bare behaviour reads as a clean negative
result, which is why it went unnoticed the first time.

**`family_job_runner.py` — harbor API compatibility.** Tolerates both the older
and newer harbor `Job` construction APIs, and accepts a dataset path that
points directly at a single task rather than at a directory of tasks.

## Arms and routing

Configs are generated, never committed:

```bash
recuris skillflow render-configs --arm bare  --model <model> --out configs/skillflow/generated
recuris skillflow render-configs --arm skill --model <model> --out configs/skillflow/generated \
    --routing default
```

`--routing default` maps each family to `sf-<family-slug>.j2`, falling back to
`sf-universal.j2`. `--routing frozen_insample` additionally applies six
per-family overrides from `skill_memories/skillflow/templates/ROUTING.map`
whose templates were chosen by reading those families' own scores. Those six
are **in-sample**; see the repository README. The renderer prints a warning when that
policy is selected, and the README names which policy produced which
reported number.
