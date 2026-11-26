"""测试配置：Starlette 1.10 对 httpx TestClient 的兼容提示与业务无关。

生产路径不使用 TestClient；依赖锁定 starlette==1.10.0 / httpx==0.28.1，
这里只过滤该条已知升级提示，避免每次 pytest 都出现无关 warning。
"""

from __future__ import annotations

import warnings

from starlette.exceptions import StarletteDeprecationWarning

warnings.filterwarnings(
    "ignore",
    message=r"Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.",
    category=StarletteDeprecationWarning,
)
