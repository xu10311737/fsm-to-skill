import test from "node:test";
import assert from "node:assert/strict";

import {
  normalizeCodeInputs,
  loadWorkflow,
  nodeOutHandles,
  parseReturnDictKeys,
  state,
  setGraphNodeName,
} from "../static/js/store.js";

test("normalizeCodeInputs keeps live parameter object references", () => {
  const parameter = {
    name: "arg-1",
    description: "参数描述",
    type: "string",
    required: true,
  };
  const config = { inputs: [parameter] };

  normalizeCodeInputs(config, { stripSource: true });
  assert.equal(config.inputs[0], parameter);

  parameter.name = "customer-id";
  normalizeCodeInputs(config);

  assert.equal(config.inputs[0], parameter);
  assert.equal(config.inputs[0].name, "customer-id");
});

test("renaming an auto-output node migrates dependent variable references", () => {
  const producer = {
    id: "aggregate-1",
    type: "aggregate",
    name: "Aggregate-1",
    config: { output_type: "list" },
  };
  const graph = {
    nodes: [
      producer,
      {
        id: "llm-1",
        type: "llm",
        name: "Prompt-1",
        config: {
          prompt: "保留普通文本 Aggregate-1-output；只更新 {{ Aggregate-1-output }}",
        },
      },
      {
        id: "if-1",
        type: "if",
        name: "IF-1",
        config: {
          conditions: [{
            variable: "Aggregate-1-output",
            operator: "是",
            value: "Aggregate-1-output",
            value_type: "variable",
          }],
        },
      },
      {
        id: "for-1",
        type: "for",
        name: "For-1",
        config: {
          list_source: "Aggregate-1-output",
          body: {
            nodes: [{
              id: "body-llm",
              type: "llm",
              name: "Prompt-Body",
              config: { prompt: "{{ Aggregate-1-output }}" },
            }],
            edges: [],
          },
        },
      },
      {
        id: "code-1",
        type: "code",
        name: "Code-1",
        config: { inputs: [{ name: "data", source: "Aggregate-1-output" }] },
      },
    ],
    edges: [],
  };

  const result = setGraphNodeName(graph, producer, "Merged Results");

  assert.equal(result.ok, true);
  assert.equal(producer.name, "Merged-Results");
  assert.equal(
    graph.nodes[1].config.prompt,
    "保留普通文本 Aggregate-1-output；只更新 {{ Merged-Results-output }}",
  );
  assert.equal(graph.nodes[2].config.conditions[0].variable, "Merged-Results-output");
  assert.equal(graph.nodes[2].config.conditions[0].value, "Merged-Results-output");
  assert.equal(graph.nodes[3].config.list_source, "Merged-Results-output");
  assert.equal(
    graph.nodes[3].config.body.nodes[0].config.prompt,
    "{{ Merged-Results-output }}",
  );
  assert.equal(graph.nodes[4].config.inputs[0].source, "Merged-Results-output");
});

test("renaming an error-enabled Code node updates its generated error prompt", () => {
  const producer = {
    id: "code-1",
    type: "code",
    name: "Code-1",
    config: { error_branch: true },
  };
  const prompt = {
    id: "llm-1",
    type: "llm",
    name: "Error-Prompt-1",
    config: {
      prompt: "Code 节点「Code-1」执行失败。\n" +
        "错误类型：{{ Code-1-error-type }}\n" +
        "错误信息：{{ Code-1-error-message }}",
    },
  };
  const graph = {
    nodes: [producer, prompt],
    edges: [{
      id: "edge-1",
      source: "code-1",
      target: "llm-1",
      source_handle: "error",
    }],
  };

  setGraphNodeName(graph, producer, "Fetch Data");

  assert.equal(
    prompt.config.prompt,
    "Code 节点「Fetch-Data」执行失败。\n" +
      "错误类型：{{ Fetch-Data-error-type }}\n" +
      "错误信息：{{ Fetch-Data-error-message }}",
  );
});

test("parseReturnDictKeys only reads return dictionaries from main", () => {
  const code = `
def helper():
    return {"helper": "ignored"}

def main(params):
    def nested():
        return {"nested": "ignored"}
    if params.get("ok"):
        return {
            "accepted": True,
        }
    return {"rejected": False}
`;

  assert.deepEqual(parseReturnDictKeys(code), ["accepted", "rejected"]);
});

test("loaded IF nodes use one handle per condition and preserve ELSE", () => {
  loadWorkflow({
    name: "IF migration",
    nodes: [{ id: "start-1", type: "start", config: {} }, {
      id: "if-1", type: "if", config: {
        conditions: [{ variable: "a", operator: "是", value: "1" },
          { variable: "b", operator: "是", value: "2" }],
      },
    }],
    edges: [{ source: "start-1", target: "if-1", source_handle: "out" }],
  });
  const ifNode = state.workflow.nodes.find((node) => node.id === "if-1");
  assert.deepEqual(nodeOutHandles(ifNode), ["if-1", "if-2", "else"]);
  assert.equal(ifNode.config.combinator, undefined);
});

test("loaded loop body remains editable after normalizing nested nodes", () => {
  loadWorkflow({
    name: "Loop body",
    nodes: [{ id: "for-1", type: "for", config: {
      body: { nodes: [{ id: "body-if", type: "if", config: {
        variable: "item", operator: "不为空",
      } }], edges: [] },
    } }],
    edges: [],
  });
  const loop = state.workflow.nodes[0];
  assert.equal(loop.config.body.nodes[0].config.branch_mode, "multi");
  assert.deepEqual(loop.config.body.nodes[0].config.conditions, [{
    variable: "item", operator: "不为空", value: "", value_type: "constant",
  }]);
});
