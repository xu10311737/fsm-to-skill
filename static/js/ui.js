/* fsm to skill - 通用 UI 辅助：模态框 / Toast / 表单 */

export function openModal({ title, body, footer, wide, className, onClose }) {
  const modal = document.getElementById("modal");
  const mTitle = document.getElementById("modal-title");
  const mBody = document.getElementById("modal-body");
  const mFooter = document.getElementById("modal-footer");
  const box = modal.querySelector(".modal-box");
  mTitle.textContent = title || "";
  mBody.innerHTML = "";
  mFooter.innerHTML = "";
  if (typeof body === "string") mBody.innerHTML = body;
  else if (body) mBody.append(body);
  if (footer) {
    for (const btn of footer) {
      const b = document.createElement("button");
      b.className = `btn ${btn.kind || ""}`.trim();
      b.textContent = btn.label;
      if (btn.id) b.id = btn.id;
      b.addEventListener("click", () => btn.action(closeModal));
      mFooter.append(b);
    }
  } else {
    mFooter.classList.add("hidden");
  }
  if (footer) mFooter.classList.remove("hidden");
  box.className = "modal-box";
  box.classList.toggle("wide", !!wide);
  if (className) box.classList.add(className);
  modal.classList.remove("hidden");
  modal._onClose = onClose || null;
  return closeModal;
}

export function closeModal() {
  const modal = document.getElementById("modal");
  if (modal.classList.contains("hidden")) return;
  modal.classList.add("hidden");
  const fn = modal._onClose;
  modal._onClose = null;
  if (fn) fn();
}

export function toast(message, kind = "info", duration = 3200) {
  const wrap = document.getElementById("toast");
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  wrap.append(el);
  setTimeout(() => {
    el.style.opacity = "0";
    el.style.transition = "opacity .3s";
    setTimeout(() => el.remove(), 320);
  }, duration);
}

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k === "value") node.value = v;
    else if (k === "checked") node.checked = !!v;
    else if (k === "disabled") node.disabled = !!v;
    else node.setAttribute(k, v);
  }
  for (const c of children) {
    if (c == null) continue;
    node.append(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return node;
}

/** 按变量类型解析表单输入值 */
export function parseTypedValue(type, raw) {
  switch (type) {
    case "int": {
      const v = parseInt(raw, 10);
      if (Number.isNaN(v)) throw new Error(`"${raw}" 不是有效的 int`);
      return v;
    }
    case "float": {
      const v = parseFloat(raw);
      if (Number.isNaN(v)) throw new Error(`"${raw}" 不是有效的 float`);
      return v;
    }
    case "bool":
      if (typeof raw === "boolean") return raw;
      return raw === "true" || raw === "1" || raw === "是";
    case "list":
    case "dict": {
      if (raw == null || raw === "") return type === "list" ? [] : {};
      const v = JSON.parse(raw);
      if (type === "list" && !Array.isArray(v)) throw new Error("需要 JSON 数组");
      if (type === "dict" && (typeof v !== "object" || Array.isArray(v)))
        throw new Error("需要 JSON 对象");
      return v;
    }
    default:
      return raw == null ? "" : String(raw);
  }
}

/** 为 typed 变量生成一个输入控件 */
export function typedInput(type, value = "") {
  if (type === "bool") {
    return el("select", { class: "input", value: String(value) },
      el("option", { value: "true", text: "true" }),
      el("option", { value: "false", text: "false" }));
  }
  if (type === "list" || type === "dict") {
    const ta = el("textarea", {
      class: "input mono", rows: "3",
      placeholder: type === "list" ? '["a", "b"]' : '{"k": "v"}',
    });
    ta.value = typeof value === "string" ? value : JSON.stringify(value ?? "");
    return ta;
  }
  return el("input", {
    class: "input",
    type: type === "int" || type === "float" ? "number" : "text",
    value: value == null ? "" : String(value),
    step: type === "float" ? "any" : undefined,
  });
}

/** textarea 变量插入助手：输入 "/" 弹出变量列表，选中插入 {{ var }} */
export function attachVarHelper(textarea, getVars) {
  let box = null;
  let triggerStart = -1;

  const closeBox = () => { if (box) { box.remove(); box = null; } triggerStart = -1; };

  textarea.addEventListener("input", () => {
    const pos = textarea.selectionStart;
    const text = textarea.value;
    if (triggerStart < 0) {
      if (text[pos - 1] === "/") {
        triggerStart = pos - 1;
        openBox("");
      }
    } else {
      const frag = text.slice(triggerStart + 1, pos);
      if (pos <= triggerStart || /[\s/]/.test(frag)) closeBox();
      else openBox(frag);
    }
  });
  textarea.addEventListener("blur", () => setTimeout(closeBox, 200));
  textarea.addEventListener("keydown", (ev) => {
    if (!box) return;
    const items = [...box.querySelectorAll(".var-item")];
    let idx = items.findIndex((i) => i.classList.contains("active"));
    if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
      ev.preventDefault();
      idx = ev.key === "ArrowDown"
        ? Math.min(items.length - 1, idx + 1)
        : Math.max(0, idx - 1);
      items.forEach((i, k) => i.classList.toggle("active", k === idx));
    } else if (ev.key === "Enter" || ev.key === "Tab") {
      if (idx >= 0 && items[idx]) {
        ev.preventDefault();
        pick(items[idx].dataset.name);
      }
    } else if (ev.key === "Escape") closeBox();
  });

  function openBox(filter) {
    const vars = (getVars() || []).filter((v) =>
      !filter || v.name.toLowerCase().includes(filter.toLowerCase()));
    closeBoxOnly();
    if (!vars.length) return;
    box = el("div", { class: "var-helper" });
    vars.slice(0, 12).forEach((v, i) => {
      const item = el("div", {
        class: "var-item" + (i === 0 ? " active" : ""),
        "data-name": v.name,
      },
        el("span", { class: "var-name", text: v.name }),
        el("span", { class: "var-type", text: v.type || "" }));
      item.addEventListener("mousedown", (ev) => {
        ev.preventDefault();
        pick(v.name);
      });
      box.append(item);
    });
    textarea.parentElement.style.position = "relative";
    textarea.parentElement.append(box);
  }
  function closeBoxOnly() { if (box) { box.remove(); box = null; } }

  function pick(name) {
    const pos = textarea.selectionStart;
    const before = textarea.value.slice(0, triggerStart);
    const after = textarea.value.slice(pos);
    const insert = `{{ ${name} }}`;
    textarea.value = before + insert + after;
    const caret = before.length + insert.length;
    textarea.selectionStart = textarea.selectionEnd = caret;
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
    closeBox();
    textarea.focus();
  }
}
