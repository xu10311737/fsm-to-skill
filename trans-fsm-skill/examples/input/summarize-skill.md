---
name: text-summarizer
description: 对一段文本进行摘要、提取要点，并可选择输出语言。
---

# Text Summarizer（示例：用户已写好的 SKILL）

根据输入的文本，按以下流程处理：

1. 读取输入文本 `text`，若为空则返回错误。
2. 调用大模型生成一段中文摘要（不超过 200 字）。
3. 从原文提取 3~5 个要点，以列表形式返回。
4. 若用户指定 `language`（如 `en`），将摘要翻译为该语言。
5. 返回 `{summary, bullet_points, language}`。

## 使用
将文本作为 `text` 传入，可选 `language` 指定输出语言。

## 输出
- `summary`：摘要文本
- `bullet_points`：要点列表
- `language`：实际使用的语言