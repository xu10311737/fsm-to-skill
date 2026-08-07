# fsm-to-skill · Transform Complex Flows into a State-Machine-Controlled SKILL

> Let the LLM return to its "reasoning" job, and hand "flow control" back to the classic, rigorous finite state machine (FSM).

> A fully local, lightweight SKILL orchestration and generation tool based on a state machine (FSM). Build flows on a canvas, debug locally, and export Skills with one click.

[中文](./README.md) · [Documentation](./static/docs.html) · [License](./LICENSE)

---

## Interface Preview

![Interface Preview](images/screenshot-build.png)

![Interface Preview](images/screenshot-run.png)

---

## Why fsm-to-skill

---

**When an Agent Skill gets complex...**

**Flow control lost & hallucination**:
Complex Skills inevitably contain various branch decisions and flow constraints. As redundant and conflicting descriptions pile up, the AI works harder and harder to fix them. The bigger and more complex the Skill, the easier it is to induce reward hacking and logical hallucination in the LLM.

**Maintenance cost**:
A complex Skill has no clear logical boundary. Once the business logic changes, facing hundreds or thousands of lines of prompts, developers struggle to maintain and iterate effectively. The AI works harder and harder, and the SKILL grows bigger and bigger.

**Key constraints forgotten**:
Once the execution chain gets too long, the LLM's attention scatters and important constraints get ignored. For example, when asked to execute 100 similar but complex tasks one by one, the LLM often fails to do each rigorously and tends to miss key constraints or skip steps.

---

**Core idea**:

- The state machine is the sole flow controller; the flow is entirely driven by the state machine.
- Single entry: the state machine is the only entry for Agent interaction, producing standard SOP guidance and a dedicated Prompt for each Step.
- On-demand presentation: at each step the Agent only sees what it needs to see, lowering cognitive load and thoroughly avoiding forgotten key constraints.
- Input validation: each step strictly validates the Agent's output and loops the validation result back to the Agent for correction.

Pain points fsm-to-skill solves:

- Complex inference flows scattered across hand-written prompts — hard to reuse or compose.
- Wanting an LLM Agent to have deterministic, multi-step, branchable, loopable, and computable flows without writing a state machine.
- After a SKILL is written, debugging is opaque — you don't know which step drifted.
- Orchestration results are hard to persist as shareable, versionable Skill assets.

---

## Key Features

- **Fully local**: no cloud dependency; data and keys stay on your machine.
- **7 node types**: `start / code / llm / if / for / aggregate / end` — everything you need for orchestration.
- **Visual orchestration**: drag-and-drop wiring, real-time validation, one-click error-node location.
- **Real LLM debugging**: visually run the flow with streaming output, token stats, and node-level variable snapshots.
- **One-click Skill export**: generates `SKILL.md`, `agent_interface.json`, `workflow.yaml`, `inference/`, `scripts/`.
- **State-machine driven**: the exported `scripts/main.py` is a self-contained state-machine thread — the Agent just runs one command to enter the DAG.
- **Structural validation**: acyclic, single start, variable traceability, type matching, no nested For, and more.
- **SKILL conversion**: convert an already-written complex business-process SKILL into an fsm skill that opens in the app.
- **Stability guarantees**: code execution error handling, node retry counts, state-machine wait-timeout configuration, and more.

---

## Steps

### Requirements

- Python **3.10+** (3.12+ recommended)

### Installation

```bash
# 1. Clone the repository
git clone https://github.com/xu10311737/fsm-to-skill.git
cd fsm-to-skill

# 2. Create a virtual environment (optional but recommended)
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. (Optional) dev / test dependencies
pip install -r requirements-dev.txt
```

### Run

```bash
python run.py
```

The browser opens `http://localhost:8000` automatically (default port; adjustable in `run.py`).

---

## Quick Start

1. **Configure**: in the Config page, fill in the provider `base_url` and `api_key` and set a default model; optionally specify a Python interpreter path if you plan to run Code nodes.
2. **Orchestrate**: in the editor canvas, click/drag components from the left panel onto the canvas; click the `+` on a node's right edge to append, then drag from `+` onto a target input port to wire.
3. **Configure nodes**: select a node and set parameters in the right panel; validate anytime with the toolbar's Validate button.
4. **Run**: switch to the Run tab, fill in the input variables declared by the start node, click ▶ and watch node states in real time.
5. **Export**: once validation passes, click Export SKILL to generate the Skill directory.

---

## Node Types

| Node | Description |
| --- | --- |
| **start** | The DAG entry of every workflow; declares input variables. |
| **code** | Runs Python in a subprocess. Inputs use an argparse-style param schema; the returned dict automatically becomes output variables; syntax errors are auto-checked, with single-node debugging. Supports error handling: retry / error branch. |
| **llm** | Renders a prompt, supports inserting variables. The engine itself does not call the model; it acts as the Agent exit returning a message. |
| **if** | Conditional branch: supports one or more IF exits. |
| **for** | Iterates a list variable; a sub-canvas orchestrates the loop body. |
| **aggregate** | Merges multiple successful upstreams by type (string join / numeric sum / list concat / dict merge). |
| **end** | Any end hit terminates the whole workflow immediately — great for early exit on error branches. |

---

## Run & Debug

- **Live states**: node states tracked in real time, with per-node elapsed time.
- **Node details**: inspect Prompt, reasoning, stdout/stderr, errors, and produced variables.
- **Stats**: total time, node counts by state, LLM calls & token usage, variable snapshots.
- **Logs**: full engine event stream; export logs as text.
- **Single-node debug**: run a Code node alone with explicit params to quickly verify script logic.

---

## Export a SKILL

Once validation passes, choose a target directory and export:

```
my-skill/
├── SKILL.md              # doc (input table / outputs / structure)
├── agent_interface.json  # routing table from Agent to Code / Prompt
├── workflow.yaml         # workflow definition
├── inference/            # LLM prompt templates
└── scripts/              # Code-node scripts + auto-generated main.py state-machine entry
```

---

## Agent / Code / Prompt Interaction Model

- `scripts/main.py` is the total state-machine thread of the exported Skill;
- **code node = entry**: the total thread routes the input to the entry of each code node and executes it.
- **Prompt node = exit**: the engine pauses at a Prompt node, rendering the string and returning it to the Agent; the Agent's next call re-enters through a Code node.

Agent invocation:

```bash
python main.py --task-id task-001 --step-id code-1 --step-param '{"arg-1":"hello"}'
```

- `--task-id`: identifies a single task.
- `--step-id`: which Code node the input enters.
- `--step-param`: input JSON string.

The state machine continues executing the DAG until it hits a Prompt exit and returns the rendered string to the Agent.

---

## Project Structure

```
fsm-to-skill/
├── app/                    # back-end source
│   ├── engine/             # DAG state-machine execution engine
│   ├── services/           # debug service, Agent runtime, config store, etc.
│   ├── validator/          # structural validations
│   └── web/                # routes / API
├── static/                 # pure static frontend (canvas, run, config)
│   ├── index.html          # main UI
│   ├── docs.html           # documentation
│   └── js/ css/
├── data/
│   ├── config.yaml         # local config
│   └── workflows/          # local workflows
├── trans-fsm-skill/        # prebuilt skills: convert an existing SKILL into an fsm skill
├── work/                   # runtime artifacts / debug scripts
├── main.py                 # FastAPI entry
├── run.py                  # launcher
├── requirements.txt        # runtime deps
├── requirements-dev.txt    # test deps
├── README.md
└── LICENSE
```

---

## License

[MIT](./LICENSE)