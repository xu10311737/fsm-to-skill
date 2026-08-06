"""本地启动入口。

用法：
    python run.py            # 直接启动（默认 127.0.0.1:8000）
    uvicorn run:app --reload # 开发模式（热重载）

数据目录默认为 backend/data（可用环境变量 DAG2SKILL_DATA_DIR 覆盖）。
"""
from __future__ import annotations

import os
from pathlib import Path

from app.main import create_app

DATA_DIR = Path(os.environ.get("DAG2SKILL_DATA_DIR",
                               Path(__file__).resolve().parent / "data"))

app = create_app(DATA_DIR)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("run:app", host="127.0.0.1", port=8000, reload=False)
