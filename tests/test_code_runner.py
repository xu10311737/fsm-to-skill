"""Code 子进程执行器测试（PRD 4.2 + 第 7 章）。

契约：app.services.code_runner.run_code(code, args, timeout=30,
                                        python_path=None) -> dict
返回: {"ok", "result", "stdout", "stderr", "error_type", "error_message",
       "duration_ms"}
真实子进程执行：stdout 捕获、超时、异常分类都覆盖。
"""
import sys

import pytest

from app.services.code_runner import run_code  # TDD：尚不存在

PY = sys.executable


class TestRunCode:
    def test_basic_success(self):
        r = run_code("def main(a, b):\n    return {\"result\": a + b}\n",
                     {"a": 2, "b": 3}, python_path=PY)
        assert r["ok"] is True
        assert r["result"] == {"result": 5}
        assert r["error_type"] is None
        assert r["duration_ms"] >= 0

    def test_stdout_captured(self):
        r = run_code(
            "def main():\n    print(\"第一行\")\n    print(\"第二行\")\n"
            "    return {\"result\": 1}\n",
            {}, python_path=PY)
        assert r["ok"] is True
        assert "第一行" in r["stdout"]
        assert "第二行" in r["stdout"]

    def test_stderr_captured(self):
        r = run_code(
            "def main():\n    import sys\n    sys.stderr.write(\"警告\\n\")\n"
            "    return {\"result\": 1}\n",
            {}, python_path=PY)
        assert r["ok"] is True
        assert "警告" in r["stderr"]

    def test_runtime_exception(self):
        r = run_code("def main():\n    raise ValueError(\"坏了\")\n",
                     {}, python_path=PY)
        assert r["ok"] is False
        assert r["error_type"] == "ValueError"
        assert "坏了" in r["error_message"]
        assert r["result"] is None

    def test_syntax_error(self):
        r = run_code("def main(:\n", {}, python_path=PY)
        assert r["ok"] is False
        assert r["error_type"] == "SyntaxError"

    def test_missing_main(self):
        r = run_code("x = 1\n", {}, python_path=PY)
        assert r["ok"] is False
        assert r["error_type"] in ("NameError", "KeyError", "RuntimeError")

    def test_timeout(self):
        r = run_code(
            "def main():\n    import time\n    time.sleep(5)\n"
            "    return {\"result\": 1}\n",
            {}, timeout=1, python_path=PY)
        assert r["ok"] is False
        assert r["error_type"] in ("TimeoutError", "Timeout")
        assert r["duration_ms"] < 4000

    def test_non_serializable_result(self):
        """返回值无法 JSON 序列化时标记失败。"""
        r = run_code(
            "def main():\n    return {\"result\": object()}\n",
            {}, python_path=PY)
        assert r["ok"] is False

    def test_args_passed_by_name(self):
        r = run_code(
            "def main(name, count):\n"
            "    return {\"result\": f\"{name}x{count}\"}\n",
            {"name": "n", "count": 7}, python_path=PY)
        assert r["result"] == {"result": "nx7"}

    def test_hyphen_args_pass_into_params_dict(self):
        r = run_code(
            "def main(params):\n"
            "    return {\"result\": params[\"arg-1\"].upper()}\n",
            {"arg-1": "ok"}, python_path=PY)
        assert r["ok"] is True
        assert r["result"] == {"result": "OK"}

    def test_legacy_python_param_name_still_works(self):
        r = run_code(
            "def main(arg_1):\n"
            "    return {\"result\": arg_1.upper()}\n",
            {"arg-1": "ok"}, python_path=PY)
        assert r["ok"] is True
        assert r["result"] == {"result": "OK"}

    def test_unicode_roundtrip(self):
        r = run_code("def main(s):\n    return {\"result\": s + \"！\"}\n",
                     {"s": "中文"}, python_path=PY)
        assert r["result"] == {"result": "中文！"}

    def test_complex_types_roundtrip(self):
        r = run_code(
            "def main(payload):\n"
            "    return {\"result\": {\"keys\": sorted(payload.keys())}}\n",
            {"payload": {"b": [1, 2], "a": {"x": 1}}}, python_path=PY)
        assert r["result"] == {"result": {"keys": ["a", "b"]}}

    def test_isolation_between_calls(self):
        """两次调用互不影响（独立子进程）。"""
        run_code("def main():\n    import os\n"
                 "    os.environ['ISO_T'] = '1'\n"
                 "    return {\"result\": 1}\n", {}, python_path=PY)
        r2 = run_code(
            "def main():\n    import os\n"
            "    return {\"result\": os.environ.get('ISO_T', 'none')}\n",
            {}, python_path=PY)
        assert r2["result"] == {"result": "none"}


class TestStaticCheck:
    def test_static_check_valid(self):
        from app.services.code_runner import static_check
        ok, msg = static_check("def main(a):\n    return {\"result\": a}\n")
        assert ok is True

    def test_static_check_syntax(self):
        from app.services.code_runner import static_check
        ok, msg = static_check("def main(:\n")
        assert ok is False
        assert msg

    def test_static_check_requires_main(self):
        from app.services.code_runner import static_check
        ok, msg = static_check("def other():\n    pass\n")
        assert ok is False
