---
name: trans-fsm-skill
title: 将已有 SKILL 转换为 fsm to skill 标准格式
description: 读取一个已经编写好的业务 SKILL.md，将其转换为符合本项目（fsm to skill）标准的可运行 skill 包（SKILL.md + workflow.yaml + scripts/main.py + agent_interface.json）。转换由 Agent 根据本指南执行，并借助 scripts/ 下的辅助脚本生成骨架。
version: 1.0.0
trigger: 用户提供一份已写好的 SKILL.md（或粘贴其内容），要求转换为 fsm skill
---

# trans-fsm-skill

你是一个「skill 转换器」。你的任务是把用户已经写好的一个业务 SKILL.md，转换为符合 **fsm to skill** 项目标准的 skill 包。

## 目标格式（输出）

一个标准的 fsm to skill 包包含以下文件，输出到 `outputs/<skill-slug>/`：

```
outputs/<skill-slug>/
├── SKILL.md                 # 重新组织的技能说明（含 frontmatter）
├── workflow.yaml            # 状态机工作流（yaml：id/name/description/nodes/edges）
├── agent_interface.json     # 路由表（agent/command 选择哪个节点）
├── scripts/
│   └── main.py              # 状态机执行线程（含 SKILL.md 执行入口）
└── inference/               # 可选：推理/生成配置
    └── ...
```

详细字段规范见下方「标准格式速查」。

## 转换步骤

### 第 1 步：阅读并理解源 SKILL
- 通读用户提供的 SKILL.md，提取：
  - 技能名称（name）、一句话描述（description）
  - 主要能力清单（把技能拆成若干「能力」或「步骤」）
  - 输入（用户给出什么）、输出（返回什么）
  - 关键规则/分支/循环逻辑

### 第 2 步：设计状态机
将源 SKILL 的流程建模为一条**有向无环图（DAG）**，从 Start 到 End。常用节点类型：

| 类型 | 用途 | 关键 config 字段 |
|------|------|------------------|
| `start` | 入口，声明输入变量 | `inputs: [{name, type}]` |
| `code` | 计算/数据处理，含 `def main(...)` | `inputs/outputs`, `code` |
| `for` | 循环遍历 list | `list_source`, `collect`, `body{nodes,edges}` |
| `if`  | 条件分支 | `conditions:[{variable, operator, value, value_type}]` |
| `llm` | 调用大模型 | `prompt`（支持 `{{ var }}` 模板） |
| `aggregate` | 聚合循环结果 | `operation`, `output_type`, `inputs` |
| `end`  | 出口 | 通常为空 |

**建模原则**：
- 每个节点必须有唯一 `id` 和 `name`。
- `code` 节点必须定义 `def main(...)` 并返回 dict。
- 只有 `start` 能声明输入、只能有 1 个 `start` 和 1 个 `end`。
- 循环体（`for.body`）内**不允许再嵌套 for**。
- 若源 SKILL 是问答型/对话型，优先用 `start → llm → end` 的简单结构，把提示词模板化。

### 第 3 步：生成 workflow.yaml
使用辅助脚本生成骨架，然后填充节点与边：

```bash
python scripts/main.py init --name "技能名" --desc "一句话描述" --slug my-skill
```

生成后手工/由你编辑填充：
- `start` 的 `inputs`（变量名用 ASCII 标识符，类型限 `string/int/float/bool/list/dict/any`）
- 各 `code` 节点的输入（`source` 指向上游产出的变量名）与 `code`（`def main`）
- `llm` 节点的 `prompt`（用 `{{ 上游变量 }}` 引用）
- `for` 的 `list_source` 与 `body` 子图
- `if` 的 `conditions` 与分支出边 `source_handle: if / else`
- `aggregate` 汇总循环输出
- `edges` 连接各节点（`source_handle: out`）

### 第 4 步：生成 agent_interface.json
决定路由：该 skill 被 agent 调用时，`command` 或 `agent` 触发哪个节点。

```bash
python scripts/main.py iface --slug my-skill --entry llm-node-id [--command cmd-name]
```

### 第 5 步：生成 scripts/main.py（状态机线程）
```bash
python scripts/main.py engine --slug my-skill --workflow workflow.yaml
```

该脚本会生成一个可导入的 `main.py`，内含：
- `run_workflow(request)`：按 workflow 顺序执行节点
- 状态机线程（start → ... → end）
- 每个 `code` 节点的 `def main` 执行体
- SKILL.md 执行入口（读取同目录 SKILL.md 作为上下文）

### 第 6 步：编写 SKILL.md
输出目录内的 `SKILL.md` 需带 frontmatter 并重新组织为清晰结构：
```markdown
---
name: <skill-name>
description: <一句话>
---
# <技能名>
## 功能
## 使用方法
## 参数说明
## 输出说明
```

### 第 7 步：校验
```bash
python scripts/main.py validate --workflow workflow.yaml
```
确保无错误（至少无 `NO_START/NO_END/DUPLICATE_NODE_ID/CYCLE/UNREACHABLE_NODE/IF_BRANCH_UNCONNECTED/CODE_SYNTAX/NO_MAIN_FUNC` 等）。

## 标准格式速查

### workflow.yaml 骨架
```yaml
id: my-skill
name: 技能名
description: 一句话描述
nodes:
- id: start
  type: start
  name: start
  config:
    inputs:
    - name: question
      type: string
  position: {x: 80, y: 200}
- id: main
  type: llm
  name: main
  config:
    prompt: "你是助手。问题：{{ question }}"
  position: {x: 340, y: 200}
- id: end
  type: end
  name: end
  config: {}
  position: {x: 600, y: 200}
edges:
- {id: e1, source: start, target: main, source_handle: out}
- {id: e2, source: main, target: end, source_handle: out}
```

### code 节点契约
- 必须定义 `def main(...)`，参数与 `config.inputs[].name`（映射为 Python 形参）一一对应。
- 返回 `dict`，键需与 `config.outputs[].name` 对应（或静态 return 字面量键）。
- 形参名：输入名中 `-` 会映射为 `_`，可与 `def main` 用 `params` 单参 dict 形式。

### for 循环
```yaml
- id: loop
  type: for
  name: loop
  config:
    list_source: items        # 上游产出的 list 变量
    collect: results          # 聚合输出名
    body:
      nodes: [ ...子图节点... ]
      edges: [ ...子图边... ]
```
循环体可用局部变量：`item`（当前元素）、`index`、`len`、`total`。

### if 分支
```yaml
- id: decision
  type: if
  name: decision
  config:
    conditions:
    - {variable: score, operator: ">", value: "60", value_type: constant}
```
出边：通过分支的边用 `source_handle: if`，否则分支用 `source_handle: else`。
多条件时用 `if-1`、`if-2`… `else`。

### aggregate
```yaml
- id: join
  type: aggregate
  name: join
  config:
    operation: join
    output_type: string
    inputs:
    - {name: data, source: loop-output, type: list}
```

## 常见约束（务必遵守）
- 节点 `id` 唯一；`name` 唯一（非空）。
- `start` 无入边、无前驱；`end` 无出边。
- 全流程必须可达（单一 Start，无环）。
- LLM 节点 `prompt` 不能为空；引用的 `{{ var }}` 必须已定义。
- 变量名：ASCII 标识符，非保留字（`TYPES = {string,int,float,bool,list,dict,any}`）。
- `code` 的 `main` 形参与声明输入一一对应（或单参 `params`）。
- 禁止 `for` 嵌套 `for`。

## 输出
最后向用户交付：
1. 输出的 skill 包路径及文件清单
2. 简要说明：原 SKILL 被拆分成了哪些节点、如何映射
3. 若转换中有无法自动映射的步骤（如依赖外部服务），明确列出需要人工补充的地方