"""text-summarizer 状态机执行线程（示例转换输出）。

流程：start → summarize(llm) → extract(code) → translate(llm) → end
"""
from __future__ import annotations

import json
import re
import sys


WORKFLOW_ID = "text-summarizer"


class FakeLLM:
    """占位 LLM —— 真实部署时替换为对项目 LLM 节点的调用。"""

    def generate(self, prompt: str) -> str:
        # 示例：从 prompt 中截取文本，做简单去空白
        text = prompt.split("文本：")[-1].strip()
        return text[:200] if text else "（无内容）"


_llm = FakeLLM()


def _render(template: str, ctx: dict) -> str:
    def _sub(m):
        name = m.group(1).strip()
        return str(ctx.get(name, m.group(0)))
    return re.sub(r"\{\{\s*(\w+)\s*\}\}", _sub, template)


def summarize(ctx: dict) -> str:
    prompt = _render(
        "请对以下文本生成不超过 200 字的中文摘要。\n\n文本：\n{{ text }}",
        ctx,
    )
    return _llm.generate(prompt)


def extract(summary: str) -> list:
    lines = [line.strip() for line in summary.split("。") if line.strip()]
    return lines[:5]


def translate(ctx: dict, summary: str) -> str:
    prompt = _render(
        "将以下摘要翻译为 {{ language }} 语言。\n\n摘要：\n{{ summary }}",
        {**ctx, "summary": summary},
    )
    return _llm.generate(prompt)


def run_workflow(request: dict) -> dict:
    ctx = {"text": request.get("text", ""), "language": request.get("language", "zh")}
    if not ctx["text"]:
        return {"error": "text 不能为空"}

    summary = summarize(ctx)
    bullet_points = extract(summary)
    language = ctx["language"]
    if language and language.lower() != "zh":
        summary = translate(ctx, summary)

    return {
        "summary": summary,
        "bullet_points": bullet_points,
        "language": language,
    }


def main() -> int:
    request = json.loads(sys.stdin.read() or "{}")
    result = run_workflow(request)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())