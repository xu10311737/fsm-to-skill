---
name: text-summarizer
description: 对文本生成摘要、提取要点，并可按指定语言输出。
---

# Text Summarizer

## 功能
- 输入一段文本，生成中文摘要（≤200 字）。
- 提取 3~5 个要点。
- 可选指定输出语言，将摘要翻译为目标语言。

## 使用方法
传入 `text`（必填）与 `language`（可选，如 `en`）。

## 参数说明
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| text | string | 是 | 待处理的文本 |
| language | string | 否 | 输出语言，默认 `zh` |

## 输出说明
- `summary`：摘要文本
- `bullet_points`：要点列表
- `language`：实际使用的语言

## 状态机
见 `workflow.yaml`：`start → summarize(llm) → extract(code) → translate(llm) → end`。