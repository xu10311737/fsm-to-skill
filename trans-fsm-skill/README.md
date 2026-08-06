# trans-fsm-skill

一个**给 Agent 解析的转换 skill**（非独立程序）。将用户已经编写好的业务 SKILL.md，转换为符合 **fsm to skill** 项目标准的可运行 skill 包。

## 它的工作方式

把这个 `trans-fsm-skill/` 目录放到支持 skill 的 Agent 环境（如 Codex）中。Agent 读取本目录的 `SKILL.md` 作为转换指南，按步骤：

1. 阅读并理解用户的源 SKILL.md
2. 将其建模为状态机（从 Start 到 End 的一条 DAG）
3. 用 `scripts/main.py` 生成骨架（workflow.yaml）
4. 生成 `agent_interface.json` 路由表
5. 生成 `scripts/main.py` 状态机线程
6. 重写 `SKILL.md`
7. 校验

## 目录结构

```
trans-fsm-skill/
├── SKILL.md                 # 转换指南（Agent 读取的核心）
├── scripts/
│   └── main.py              # 辅助脚本：init / iface / engine / validate
├── examples/
│   ├── input/               # 示例源 SKILL.md（用户已写好的）
│   └── output/              # 转换后的标准 skill 包示例
└── README.md
```

## 辅助脚本用法

```bash
# 1. 初始化骨架（生成 outputs/<slug>/workflow.yaml + SKILL.md）
python scripts/main.py init --name "技能名" --desc "一句话描述" --slug my-skill

# 2. 生成路由表
python scripts/main.py iface --slug my-skill --entry llm-node-id [--command cmd]

# 3. 生成状态机线程 scripts/main.py
python scripts/main.py engine --slug my-skill --workflow outputs/my-skill/workflow.yaml

# 4. 校验
python scripts/main.py validate --workflow outputs/my-skill/workflow.yaml
```

> `init` 只生成骨架，节点逻辑（code / prompt / if / for）需要 Agent 依据
> SKILL.md 填充。`engine` 生成的是最小可运行占位实现，供参考与扩展。

## 支持的节点类型

`start` · `code` · `for` · `if` · `llm` · `aggregate` · `end`

详细字段规范与约束见 `SKILL.md` 的「标准格式速查」。