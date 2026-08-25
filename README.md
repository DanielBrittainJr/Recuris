<a name="readme-top"></a>

<p align="center">
  <img src="assets/recuris-logo.png" alt="Recuris" width="460">
</p>

<h3 align="center">
Recursive Experiential–Working Memory Evolution for Long-Horizon Agent Harnesses
</h3>

<p align="center">
  <img src="https://img.shields.io/badge/⏳_Long--Horizon-blueviolet?style=for-the-badge" alt="Long-Horizon" />
  <img src="https://img.shields.io/badge/🔗_EM--WM_Coupling-blue?style=for-the-badge" alt="EM-WM Coupling" />
  <img src="https://img.shields.io/badge/🧊_Training--free-success?style=for-the-badge" alt="Training-free" />
  <img src="https://img.shields.io/badge/🔁_Recursive_Evolution-orange?style=for-the-badge" alt="Recursive Evolution" />
  <br><br>
  <!-- TODO: replace the # placeholders once the paper and channels are live -->
  <a href="#-citation"><img src="https://img.shields.io/badge/Paper-Coming_Soon-B31B1B.svg?logo=arxiv" alt="Paper"></a>
  <a href="#"><img src="https://img.shields.io/badge/Huggingface-DailyPaper-FFD21E.svg?logo=huggingface" alt="Hugging Face Daily Paper"></a>
  <a href="#"><img src="https://img.shields.io/badge/Coverage-Recuris-2176BC.svg?logo=x" alt="X"></a>
  <a href="#"><img src="https://img.shields.io/badge/Video-Walkthrough-FF0000.svg?logo=youtube" alt="YouTube"></a>
  <a href="https://github.com/Gen-Verse/Recuris"><img src="https://img.shields.io/badge/Code-Recuris-24292E.svg?logo=github" alt="Code"></a>
</p>

<p align="center">
  <b>National University of Singapore</b> &nbsp;·&nbsp; <b>Stanford University</b> &nbsp;·&nbsp; <b>University of Oxford</b> &nbsp;·&nbsp; <b>Princeton University</b>
</p>

---

<p align="center">
  <img src="assets/motivation.png" width="1000">
</p>

## 💡 Introduction

**Recuris** is a recursive self-improvement framework that **improves a
long-horizon agent by evolving its memory rather than its weights or its
prompt**. A frozen agent is paired with a **Skill Memory** `M = (E, W, ρ, C)`;
a meta-agent reads structured execution traces, localises each failure to one
component of that memory, and patches only that component — and a deterministic
validation gate decides on paired held-out evidence whether the patch survives.
Recuris has the following key features:

- **State-grounded memory use** — working memory drives skill invocation, so
  retrieval is conditioned on verified task state instead of a chat history that
  grows until the state is buried.
- **Targeted memory evolution** — structured trajectories `(w_t, E_t, a_t, o_t)`
  attribute a failure to a specific component, instead of nudging a monolithic
  prompt from outcomes alone.
- **Bounded by a validation gate** — candidates are admitted by paired held-out
  arithmetic and nothing else. No model votes on its own patch.
- **Training-free and model-agnostic** — the downstream agent stays frozen, and
  a memory evolved on one model transfers to others unchanged.

Overall, Recuris delivers **higher task success**, **larger gains as the horizon
grows**, and **substantially fewer long-horizon failures**, across both frontier
and open-source agents.

## 🔔 News

- **[2026-08]** 🎉 Initial release: evaluation and evolution code, the evolved
  Skill Memory packages, and the frozen evaluation splits.

## 📊 Results

Task success (`avg@4`, %), each model evaluated with the benchmark's own
reference agent alone and with that same agent plus Recuris. **Bold** marks the
better of each pair, the subscript is Δ, and † marks a paired task-clustered
bootstrap 95% CI excluding zero.

<table>
<thead>
<tr>
  <th align="left" rowspan="2">Model</th>
  <th align="center" colspan="3"><i>Cross-task evolution</i></th>
  <th align="center"><i>Within-task adaptation</i></th>
</tr>
<tr>
  <th align="right">τ²-Retail</th>
  <th align="right">τ²-Airline</th>
  <th align="right">SkillFlow</th>
  <th align="right">Terminal-Bench 2.1</th>
</tr>
</thead>
<tbody>
<tr><td colspan="5"><i>Open-source models</i></td></tr>

<tr><td align="left">Granite-4.1-3B</td>
  <td align="right">9.7</td><td align="right">34.3</td>
  <td align="right"><b>0.3</b></td><td align="right">0.6</td></tr>
<tr><td align="left">&nbsp;&nbsp;<b>+ Recuris</b></td>
  <td align="right"><b>23.0</b> <sub>+13.4†</sub></td>
  <td align="right"><b>39.8</b> <sub>+5.5</sub></td>
  <td align="right">0.0 <sub>−0.3</sub></td>
  <td align="right"><b>3.1</b> <sub>+2.5</sub></td></tr>

<tr><td align="left">Qwen3.5-4B</td>
  <td align="right">68.0</td><td align="right">75.3</td>
  <td align="right">6.0</td><td align="right">10.1</td></tr>
<tr><td align="left">&nbsp;&nbsp;<b>+ Recuris</b></td>
  <td align="right"><b>68.3</b> <sub>+0.3</sub></td>
  <td align="right"><b>79.0</b> <sub>+3.8</sub></td>
  <td align="right"><b>7.1</b> <sub>+1.1</sub></td>
  <td align="right"><b>13.0</b> <sub>+2.9</sub></td></tr>

<tr><td align="left">Qwen3.5-9B</td>
  <td align="right">77.6</td><td align="right">75.5</td>
  <td align="right">15.1</td><td align="right">17.4</td></tr>
<tr><td align="left">&nbsp;&nbsp;<b>+ Recuris</b></td>
  <td align="right"><b>79.6</b> <sub>+2.0</sub></td>
  <td align="right"><b>78.4</b> <sub>+2.9</sub></td>
  <td align="right"><b>18.4</b> <sub>+3.4</sub></td>
  <td align="right"><b>20.5</b> <sub>+3.1</sub></td></tr>

<tr><td align="left">GPT-OSS-20B</td>
  <td align="right">50.6</td><td align="right">54.8</td>
  <td align="right">7.8</td><td align="right">3.9</td></tr>
<tr><td align="left">&nbsp;&nbsp;<b>+ Recuris</b></td>
  <td align="right"><b>60.8</b> <sub>+10.2†</sub></td>
  <td align="right"><b>59.3</b> <sub>+4.5†</sub></td>
  <td align="right"><b>10.4</b> <sub>+2.6†</sub></td>
  <td align="right"><b>6.7</b> <sub>+2.8</sub></td></tr>

<tr><td align="left">Qwen3.6-27B</td>
  <td align="right">62.8</td><td align="right">79.0</td>
  <td align="right">42.2</td><td align="right">38.8</td></tr>
<tr><td align="left">&nbsp;&nbsp;<b>+ Recuris</b></td>
  <td align="right"><b>71.2</b> <sub>+8.3†</sub></td>
  <td align="right"><b>80.0</b> <sub>+1.0</sub></td>
  <td align="right"><b>58.7</b> <sub>+16.6†</sub></td>
  <td align="right"><b>42.1</b> <sub>+3.3</sub></td></tr>

<tr><td align="left">Qwen3.6-35B</td>
  <td align="right">78.2</td><td align="right">80.3</td>
  <td align="right">35.3</td><td align="right">33.1</td></tr>
<tr><td align="left">&nbsp;&nbsp;<b>+ Recuris</b></td>
  <td align="right"><b>78.5</b> <sub>+0.3</sub></td>
  <td align="right"><b>81.5</b> <sub>+1.3</sub></td>
  <td align="right"><b>48.8</b> <sub>+13.5†</sub></td>
  <td align="right"><b>36.4</b> <sub>+3.3</sub></td></tr>

<tr><td colspan="5"><i>Frontier models</i></td></tr>

<tr><td align="left">Gemini 3.7 Flash</td>
  <td align="right">73.5</td><td align="right"><b>86.5</b></td>
  <td align="right">–</td><td align="right">79.8</td></tr>
<tr><td align="left">&nbsp;&nbsp;<b>+ Recuris</b></td>
  <td align="right"><b>78.3</b> <sub>+4.8</sub></td>
  <td align="right">85.0 <sub>−1.5</sub></td>
  <td align="right">–</td>
  <td align="right"><b>82.4</b> <sub>+2.6</sub></td></tr>

<tr><td align="left">GPT-5.6 Sol</td>
  <td align="right">58.3</td><td align="right">79.0</td>
  <td align="right">–</td><td align="right">83.2</td></tr>
<tr><td align="left">&nbsp;&nbsp;<b>+ Recuris</b></td>
  <td align="right"><b>76.1</b> <sub>+17.8†</sub></td>
  <td align="right"><b>86.0</b> <sub>+7.0†</sub></td>
  <td align="right">–</td>
  <td align="right"><b>86.4</b> <sub>+3.2</sub></td></tr>

<tr><td align="left">Claude Opus 5</td>
  <td align="right">72.4</td><td align="right">89.5</td>
  <td align="right">–</td><td align="right">84.6</td></tr>
<tr><td align="left">&nbsp;&nbsp;<b>+ Recuris</b></td>
  <td align="right"><b>87.9</b> <sub>+15.6†</sub></td>
  <td align="right"><b>90.5</b> <sub>+1.0</sub></td>
  <td align="right">–</td>
  <td align="right"><b>88.4</b> <sub>+3.8</sub></td></tr>

<tr><td align="left">Doubao-2.0-Pro <sub>(deployment)</sub></td>
  <td align="right">58.1</td><td align="right">75.5</td>
  <td align="right">34.6</td><td align="right">46.1</td></tr>
<tr><td align="left">&nbsp;&nbsp;<b>+ Recuris</b></td>
  <td align="right"><b>81.4</b> <sub>+23.3†</sub></td>
  <td align="right"><b>80.5</b> <sub>+5.0</sub></td>
  <td align="right"><b>51.4</b> <sub>+16.8†</sub></td>
  <td align="right"><b>48.9</b> <sub>+2.9</sub></td></tr>
</tbody>
</table>

Recuris improves task success in **35 of the 37** completed model–benchmark
pairs, from a 3B open-source agent up to the strongest frontier models. The
largest gains reach **+23.3** on τ²-Retail and **+16.8** on SkillFlow. The
advantage grows with the interaction horizon, reaching **+32.2** on the longest
tasks, and common long-horizon failure modes fall by up to **80%**.

<p align="center">
  <img src="assets/results.png" width="1000">
</p>

<sub>For every benchmark, the Skill Memory evaluated here is the one the
evolution loop produced on the deployment model; it is then loaded and used
directly at inference, with nothing evolved during evaluation and every base
model frozen. Absolute τ² scores use our pinned judge model and are not
comparable to the public τ²-Bench leaderboard; cross-arm comparisons are,
because both arms share that judge exactly. – not evaluated on that
benchmark.</sub>

## 🛠️ Getting Started

Python 3.11 or 3.12, `git`, and — for SkillFlow and Terminal-Bench 2.1 — Docker.

```bash
git clone https://github.com/Gen-Verse/Recuris.git recuris
cd recuris
uv sync --extra all          # or: pip install -e ".[all]"
```

Put your endpoint in a `.env` at the repository root, or export it:

```bash
OPENAI_API_KEY=...
OPENAI_BASE_URL=...
```

Any OpenAI-compatible endpoint. This is needed **even for open-source arms**:
τ²-Bench scores every episode with an LLM user simulator and an LLM assertion
judge, both pinned to a reference model, and that pinning is part of the
evaluation protocol.

## 🚀 Quick Start

Every run below is a **pair** — a skill arm and a bare control that differ in
the flags shown and nothing else. Run both, or the number means nothing.

<br/>

### 🔹 τ²-Bench

```bash
bash third_party/tau2/setup.sh
uv pip install -e external/tau2-bench
recuris check-data --benchmark tau2
```

```bash
export TAU2_GATE_TERM=1 TAU2_GATE_TERM_WM=1 TAU2_STATUS_BOARD=1

export MODEL=openai/qwen3.6-27b
export ARGS='{"api_base":"http://127.0.0.1:8000/v1","api_key":"dummy","temperature":0.0,"timeout":360,"num_retries":2}'

# with Skill Memory
recuris tau2 --domain retail --agent recuris_agent --skill-memory tau2_retail \
    --open-downstream --agent-llm "$MODEL" --agent-llm-args "$ARGS" \
    --num-trials 4 --max-concurrency 4 --save-to retail_skill

# control
recuris tau2 --domain retail --agent llm_agent \
    --open-downstream --agent-llm "$MODEL" --agent-llm-args "$ARGS" \
    --num-trials 4 --max-concurrency 4 --save-to retail_bare

recuris compare --a retail_skill --b retail_bare
```

**Switching models** is just `$MODEL` and `$ARGS` — the two arms are otherwise
untouched. Serve an open-source model locally (`vllm serve <model-id> --port
8000 --served-model-name qwen3.6-27b`), or point `api_base` at a provider for
GPT / Claude / Gemini and add what it needs:

```bash
export MODEL=openai/<provider-model>
export ARGS='{"api_base":"'"$OPENAI_BASE_URL"'","api_key":"'"$OPENAI_API_KEY"'","temperature":0.0,"timeout":360,"num_retries":2,"reasoning_effort":"high","allowed_openai_params":["reasoning_effort"]}'
```

`$ARGS` must be identical between the two arms; it is validated rather than
merged, so an unknown key is an error instead of a silent drop. For **airline**,
use `--domain airline --skill-memory tau2_airline`.

### 🔹 SkillFlow

```bash
uv sync --extra skillflow && pip install huggingface_hub
bash third_party/skillflow/setup.sh
./external/SkillFlow/docker/harbor-cli-base/build.sh
python external/SkillFlow/utils/prebuild_task_images.py \
    --tasks-root external/SkillFlow/test_tasks
```

```bash
export MODEL=openai/qwen3.6-27b
export BASE=http://127.0.0.1:8000/v1

recuris skillflow render-configs --arm bare \
    --model "$MODEL" --base-url "$BASE" --out configs/skillflow/generated
recuris skillflow render-configs --arm skill --routing default \
    --model "$MODEL" --base-url "$BASE" \
    --skill-memory skillflow --out configs/skillflow/generated

# one job at a time -- concurrent harbor jobs exhaust the Docker IPv4 pool
for cfg in configs/skillflow/generated/bare_*.yaml;  do harbor run -c "$cfg" --yes; done
for cfg in configs/skillflow/generated/skill_*.yaml; do harbor run -c "$cfg" --yes; done

recuris skillflow score --bare jobs/bare --skill jobs/skill
```

Configs are generated rather than committed, so the two arms cannot drift and no
credential is ever written to disk. Switching models is again just `$MODEL`.

### 🔹 Terminal-Bench 2.1

```bash
uv sync --extra tb21
bash third_party/tb21/setup.sh
recuris check-data --benchmark tb21
```

```bash
# smoke: one task, one round
recuris tta run --taskset splits/tb21/tta_taskset_v3.json \
    --run-id smoke --arm m0 --limit 1 --rounds 1

# the three arms, all on the same attempt budget
for arm in bare m0 tta; do
    recuris tta run --taskset splits/tb21/tta_taskset_v3.json \
        --run-id demo --arm "$arm" --rounds 2 --concurrency 3
done
```

`bare` is the stock agent, `m0` adds the seed package with no learning between
attempts, and `tta` lets the meta-agent write a card after each failure that the
next attempt carries. All three get the same number of attempts — otherwise the
comparison measures retries, not adaptation.

### 🔹 Evolving a Skill Memory

The recursive loop: a meta-agent (**upstream**) reads failed trajectories from
the agent being improved (**downstream**), patches one memory component, and a
gate admits it only on paired held-out evidence.

```bash
uv sync --extra metaagent
npm install -g @anthropic-ai/claude-code
```

```bash
RECURIS_META_MODEL=...        # the upstream meta-agent's model
RECURIS_META_BASE_URL=...
RECURIS_META_API_KEY=...
```

```bash
# fails on the plumbing before you spend benchmark budget
recuris metaagent qualify --run-id qsmoke --proxy-port 4047

recuris metaagent run --domain retail --run-id retail_v1 \
    --splits splits/tau2/retail_from0_v1_k4.json \
    --rounds 4 --k 4 --arm autonomous --base neutral \
    --round-gate progressive --power-gate warn --reg-cap 1 \
    --meta-workflow hierarchical --diagnosis-workers 3 \
    --max-concurrency 6 --max-sims 1400 --proxy-port 4047
```

`--worker-model` is the downstream agent being improved and `--meta-model` is
the upstream meta-agent; both default to Doubao. To evolve a memory **for an
open-source downstream**, unfreeze the worker only — the user simulator stays
pinned, so rounds remain comparable:

```bash
recuris metaagent run --domain retail --run-id retail_gptoss_v1 \
    --splits splits/tau2/retail_from0_v1_k4.json \
    --rounds 4 --k 4 --arm autonomous --base neutral \
    --open-worker --worker-model openai/gpt-oss-20b \
    --worker-llm-args '{"api_base":"http://127.0.0.1:8000/v1","api_key":"dummy","temperature":0.0,"timeout":360,"num_retries":2,"stop_token_ids":[200002,200012]}' \
    --round-gate progressive --power-gate warn --reg-cap 1 \
    --meta-workflow hierarchical --max-concurrency 6 --proxy-port 4047
```

Evolving *for* a model beats reusing a package evolved elsewhere: on
GPT-OSS-20B a rebuilt package gained **+10.2** where the general-purpose package
transferred negatively.

> 💸 **Cost.** A campaign is days of wall-clock and thousands of model calls.
> Start with `qualify`, then a single round, before committing budget. A round
> that accepts nothing is a result — that is the gate working.

## 📖 Citation

```bibtex
@article{yu2026recuris,
  title   = {Recursive Experiential--Working Memory Evolution for Long-Horizon Agent Harnesses},
  author  = {Yu, Zhaochen and Wu, Yingcheng and Yin, Zhenfei and Chen, Kaiyuan and
             Zhao, Zhe and Wang, Mengdi and Yan, Shuicheng and Yang, Ling},
  year    = {2026},
  url     = {https://github.com/Gen-Verse/Recuris}
}
```

<p align="right">(<a href="#readme-top">back to top</a>)</p>
