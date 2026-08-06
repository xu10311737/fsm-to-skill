"""trans-fsm-skill 辅助脚本。

给 Agent 在转换过程中调用，用于生成骨架 / agent_interface / engine / 校验。

用法:
    python scripts/main.py init   --name "技能名" --desc "描述" --slug my-skill
    python scripts/main.py iface  --slug my-skill --entry llm-node-id [--command cmd]
    python scripts/main.py engine --slug my-skill --workflow workflow.yaml
    python scripts/main.py validate --workflow workflow.yaml

本脚本只生成骨架与校验，节点逻辑（code/prompt/if/for）由 Agent 依据
SKILL.md 填充。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

TYPES = {"string", "int", "float", "bool", "list", "dict", "any"}

OUTPUTS_ROOT = Path(__file__).resolve().parent.parent / "outputs"


def _slugify(text: str) -> str:
    import re
    value = re.sub(r"[^0-9a-zA-Z_-]", "-", text.strip().lower())
    value = re.sub(r"-+", "-", value).strip("-")
    return value or "skill"


def _pkg_dir(slug: str) -> Path:
    return OUTPUTS_ROOT / slug


def cmd_init(args: argparse.Namespace) -> int:
    slug = args.slug or _slugify(args.name)
    pkg = _pkg_dir(slug)
    (pkg / "scripts").mkdir(parents=True, exist_ok=True)
    (pkg / "inference").mkdir(parents=True, exist_ok=True)

    wf = {
        "id": slug,
        "name": args.name,
        "description": args.desc,
        "nodes": [
            {"id": "start", "type": "start", "name": "start",
             "config": {"inputs": [{"name": "input", "type": "string"}]},
             "position": {"x": 80, "y": 200}},
            {"id": "end", "type": "end", "name": "end",
             "config": {}, "position": {"x": 600, "y": 200}},
        ],
        "edges": [],
    }
    (pkg / "workflow.yaml").write_text(_dump_yaml(wf), encoding="utf-8")

    # 占位 SKILL.md
    (pkg / "SKILL.md").write_text(
        f"---\nname: {args.name}\ndescription: {args.desc}\n---\n\n"
        f"# {args.name}\n\n（由 trans-fsm-skill 生成，请补充用途与用法。）\n",
        encoding="utf-8",
    )

    print(f"初始化完成: {pkg}")
    print("下一步: 编辑 workflow.yaml 填充节点，然后运行 iface / engine / validate。")
    return 0


def cmd_iface(args: argparse.Namespace) -> int:
    pkg = _pkg_dir(args.slug)
    iface = {
        "schema_version": "1.0.0",
        "skill": args.slug,
        "entry": args.entry or "start",
        "routes": [],
    }
    if args.command:
        iface["routes"].append({
            "type": "command",
            "name": args.command,
            "target": args.entry or "start",
        })
    if not args.command:
        iface["routes"].append({
            "type": "agent",
            "target": args.entry or "start",
        })
    (pkg / "agent_interface.json").write_text(
        json.dumps(iface, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已生成: {pkg / 'agent_interface.json'}")
    return 0


def cmd_engine(args: argparse.Namespace) -> int:
    pkg = _pkg_dir(args.slug)
    wf = _load_yaml(Path(args.workflow) if Path(args.workflow).exists()
                    else pkg / "workflow.yaml")
    main_script = _render_engine(wf)
    (pkg / "scripts" / "main.py").write_text(main_script, encoding="utf-8")
    print(f"已生成: {pkg / 'scripts' / 'main.py'}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    wf = _load_yaml(Path(args.workflow))
    errors = _validate(wf)
    if errors:
        for err in errors:
            print(f"[错误] {err}")
        print(f"校验失败: {len(errors)} 个错误")
        return 1
    print("校验通过")
    return 0


# ---------------------------------------------------------------- 渲染 / 校验

def _dump_yaml(data: dict) -> str:
    try:
        import yaml  # type: ignore
        return yaml.safe_dump(data, allow_unicode=True, sort_keys=False,
                              default_flow_style=False)
    except ImportError:
        return _simple_dump(data, 0)


def _load_yaml(path: Path) -> dict:
    import yaml  # type: ignore
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _simple_dump(data, indent: int) -> str:
    pad = "  " * indent
    lines: list[str] = []
    if isinstance(data, dict):
        if not data:
            return "{}"
        for key, value in data.items():
            key_s = str(key)
            if isinstance(value, (dict, list)):
                lines.append(f"{pad}{key_s}:")
                lines.append(_simple_dump(value, indent + 1))
            elif isinstance(value, str):
                lines.append(f"{pad}{key_s}: {_quote(value)}")
            elif value is None:
                lines.append(f"{pad}{key_s}: null")
            else:
                lines.append(f"{pad}{key_s}: {value}")
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, (dict, list)):
                lines.append(f"{pad}-")
                lines.append(_simple_dump(item, indent + 1))
            elif isinstance(item, str):
                lines.append(f"{pad}- {_quote(item)}")
            else:
                lines.append(f"{pad}- {item}")
    return "\n".join(lines)


def _quote(text: str) -> str:
    if "\n" in text:
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return repr(text) if any(c in text for c in ":{}[],#&*!|>%@`") else text


def _render_engine(wf: dict) -> str:
    """生成一个最小可运行的状态机 main.py（供 Agent 参考/扩展）。"""
    template = r'''import json
import sys
from pathlib import Path

WORKFLOW = __WORKFLOW_JSON__

def run_workflow(request: dict) -> dict:
    """按边顺序执行节点（start -> ... -> end）。"""
    out = {"inputs": request}
    for node in WORKFLOW["nodes"]:
        ntype = node["type"]
        cfg = node.get("config", {}) or {}
        nid = node["id"]
        if ntype == "start":
            for spec in cfg.get("inputs", []) or []:
                out.setdefault(spec["name"], request.get(spec["name"]))
        elif ntype == "llm":
            prompt = cfg.get("prompt", "")
            try:
                out[nid + "-output"] = {"text": _render(prompt, out), "raw": request}
            except Exception as e:
                out[nid + "-output"] = {"error": str(e)}
        elif ntype == "code":
            out[nid + "-output"] = {"result": _run_code(cfg, out)}
        # for / if / aggregate 需按业务填充，此处仅标记
        elif ntype == "for":
            out[nid + "-output"] = {"items": out.get(cfg.get("list_source", ""))}
        elif ntype == "if":
            out[nid + "-output"] = {"branch": _eval_if(cfg, out)}
        elif ntype == "aggregate":
            out[nid + "-output"] = {"value": out.get("inputs", out.get("data"))}
        elif ntype == "end":
            out["result"] = {k: v for k, v in out.items() if k != "inputs"}
    return out.get("result", out)


def _render(template: str, ctx: dict) -> str:
    import re
    def _sub(m):
        name = m.group(1).strip()
        return str(ctx.get(name, m.group(0)))
    return re.sub(r"\{\{\s*(\w+)\s*\}\}", _sub, template)


def _run_code(cfg: dict, ctx: dict) -> dict:
    ns = {"kwargs": ctx}
    code = cfg.get("code", "def main(**kwargs):\n    return {}")
    exec(code, ns)
    sig = {}
    for spec in cfg.get("inputs", []) or []:
        sig[spec["name"]] = ctx.get(spec.get("source", spec["name"]))
    return ns["main"](**sig)


def _eval_if(cfg: dict, ctx: dict) -> str:
    cond = (cfg.get("conditions") or [{}])[0]
    var = cond.get("variable", "")
    value = cond.get("value")
    left = ctx.get(var)
    op = cond.get("operator", "==")
    try:
        ops = {
            ">": lambda a, b: a > b,
            "<": lambda a, b: a < b,
            ">=": lambda a, b: a >= b,
            "<=": lambda a, b: a <= b,
            "==": lambda a, b: a == b,
            "!=": lambda a, b: a != b,
            "是": lambda a, b: a == b,
            "包含": lambda a, b: b in (a or ""),
            "不是": lambda a, b: a != b,
        }
        ok = ops[op](left, value)
    except Exception:
        ok = False
    return "if" if ok else "else"


def main() -> int:
    """SKILL.md 执行入口：接收 JSON 请求，返回 JSON。"""
    request = json.loads(sys.stdin.read() or "{}")
    result = run_workflow(request)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''
    return template.replace("__WORKFLOW_JSON__",
                            json.dumps(wf, ensure_ascii=False, indent=2))


def _validate(wf: dict) -> list[str]:
    errors: list[str] = []
    nodes = wf.get("nodes") or []
    edges = wf.get("edges") or []
    ids = [n["id"] for n in nodes]
    names = [n.get("name", "") for n in nodes]
    if len(set(ids)) != len(ids):
        errors.append("节点 id 重复")
    if len(set(names)) != len(names):
        errors.append("节点名称重复")
    starts = [n for n in nodes if n.get("type") == "start"]
    ends = [n for n in nodes if n.get("type") == "end"]
    if len(starts) != 1:
        errors.append("Start 节点必须且只能有 1 个")
    if len(ends) != 1:
        errors.append("End 节点必须且只能有 1 个")
    return errors


def main_cli() -> int:
    parser = argparse.ArgumentParser(prog="trans-fsm-skill")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init")
    p_init.add_argument("--name", required=True)
    p_init.add_argument("--desc", default="")
    p_init.add_argument("--slug", default="")

    p_iface = sub.add_parser("iface")
    p_iface.add_argument("--slug", required=True)
    p_iface.add_argument("--entry", default="start")
    p_iface.add_argument("--command", default="")

    p_engine = sub.add_parser("engine")
    p_engine.add_argument("--slug", required=True)
    p_engine.add_argument("--workflow", default="")

    p_val = sub.add_parser("validate")
    p_val.add_argument("--workflow", required=True)

    args = parser.parse_args(args=sys.argv[1:])
    handlers = {
        "init": cmd_init,
        "iface": cmd_iface,
        "engine": cmd_engine,
        "validate": cmd_validate,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main_cli())