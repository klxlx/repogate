# RepoGate

输入一个公开 GitHub 仓库地址，先回答最基础的问题：这个仓库真实长什么样、基于哪个 commit、我们看见了哪些文件。当前完成**第 1 步（收尾 v1.4）：git/trees 元数据清单 + BLOB_CAP 采样 + 口径恒等式**，尚未实现规则、评分与报告页。

## 快速开始（Windows）

要求 Python 3.13.x（本项目在 3.13.18 上开发与验证）。

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

打开 http://127.0.0.1:8000 ，在输入框填入 `https://github.com/psf/requests` 提交。

## 快速开始（Linux / macOS）

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## 接口

| 入口 | 说明 |
|---|---|
| `GET /healthz` | 返回 `{"ok": true}` |
| `POST /api/inspect` | JSON 请求体 `{"repo_url": "https://github.com/owner/repo"}`，兼容 `url` 字段 |
| `GET /` | 单页表单，提交后同页展示结果 |

成功响应结构（requests 实例，字段名冻结）：

```json
{
  "ok": true,
  "provider": "github",
  "request_count": 3,
  "source": {"method": "trees_api", "tree_ref_fallback": false, "tree_truncated_by_github": false},
  "mode": "metadata",
  "repo": {"full_name": "psf/requests", "stars": 54768, "language": "Python", "license": "Apache-2.0", "archived": false, "pushed_at": "2026-11-02T16:55:12Z"},
  "ref": "main",
  "commit_sha": "3c7baaa05ab4aa49cc819ce3a819d260de19c157",
  "scope": {"files_scanned": 123, "files_skipped": 7, "bytes_scanned": 664038, "truncated": false, "truncate_reason": "none"},
  "stats": {"text_files": 123, "windows_script_count": 1, "total_blobs_in_tree": 130, "listed_blobs": 130, "bytes_total_listed": 4451562, "unlisted_blobs": 0},
  "timing": {"meta_ms": 1662, "commit_ms": 565, "tree_ms": 529, "tree_slow": false},
  "files": [{"path": ".coveragerc", "size": 33, "is_text": true}]
}
```

失败响应：`{"ok": false, "error": {"code": "INVALID_URL", "message": "中文提示"}}`，HTTP 状态与错误码对应：`INVALID_URL` 400、`NOT_FOUND` 404、`RATE_LIMITED` 429、`TIMEOUT` 504、`FETCH_FAILED` / `UPSTREAM_ERROR` 502。

## 取数边界

- 单仓库固定 3 次 GitHub API 请求：仓库元信息、默认分支 commit、`git/trees/{commit_sha}?recursive=1`。不 clone、不扫 Git 历史、不下载任何文件内容、不调用 contents API。
- trees 按 commit SHA 请求；仅当 SHA 返回 404/422 时回退默认分支重试一次，`source.tree_ref_fallback=true`，这是唯一允许的第 4 次请求。
- 不落盘、不解析 zip；项目目录不保留任何压缩包或解压产物。
- 清单阶段只统计 `type=blob`，单一硬顶 `BLOB_CAP=20000`（仅防病态仓库）。不再有 2000 文件 / 50MB 闸门。
- 触发 `BLOB_CAP` 时按优先级采样，不按路径字母序砍：
  1. `.env*` / `*.yaml` / `*.yml` / `*.toml` / `*.ini` / `*.json` / `*.conf`
  2. `.bat` / `.cmd` / `.ps1` / `.vbs` / `.sh` / `*.mk` / `Makefile` / `Dockerfile`
  3. `README*` / `LICENSE*` / `requirements*.txt` / `package.json` / `pyproject.toml` / `go.mod` / `pom.xml`
  4. 其余源码
  5. 其余文件
- 采样后再按路径排序返回前 50 条。
- `scope.truncate_reason` 只取三个枚举：`none`、`github_tree_limit`、`blob_cap`。GitHub 自己返回的 `truncated` 另由 `source.tree_truncated_by_github` 透出。
- 口径冻结如下（字段名不改，语义不混用）：

| 字段 | 口径 |
| --- | --- |
| `stats.total_blobs_in_tree` | 仓库 tree 里的全部 blob 数（分母） |
| `stats.listed_blobs` | 进入清单的 blob 数（受 `BLOB_CAP=20000` 限制） |
| `scope.files_scanned` | 通过过滤、真正纳入分析的文件数（文本、路径安全、单文件 ≤1MB） |
| `scope.files_skipped` | `listed_blobs - files_scanned`（媒体 / 二进制 / 超大 / 无效路径） |
| `scope.bytes_scanned` | `files_scanned` 的 `size` 之和（分析口径，不代表下载量） |
| `stats.bytes_total_listed` | `listed_blobs` 的 `size` 之和（清单口径） |
| `stats.unlisted_blobs` | `total_blobs_in_tree - listed_blobs`（因上限没看到的） |

- 恒等式（代码内 assert，单测覆盖）：
  - `files_scanned + files_skipped == listed_blobs`
  - `listed_blobs + stats.unlisted_blobs == stats.total_blobs_in_tree`
- `stats.text_files` 与 `files_scanned` 同口径；`stats.windows_script_count` 是全仓库 blob 中 `.bat/.cmd/.ps1/.vbs` 的实际数量，不受清单限额影响。
- `files` 只返回 `files_scanned` 集合中按路径排序的前 50 条。
- 耗时策略：`tree_ms > 45000` 时 `timing.tree_slow=true` 并打印 WARNING，页面顶部横幅提示「超大仓库，本次耗时较长」；`tree_ms > 90000` 才返回 `TIMEOUT`。不再在 60 秒处截杀。

## 第 1 步明确不做

规则检测、评分、三卡报告、内容抽样/下载、缓存、数据库、登录、前端框架、私有仓库 / GitLab / Gitee、CVE 漏洞库、Git 历史扫描；页面除一条耗时横幅外不展示 `stats/timing`，验收以 `/api/inspect` JSON 为准。

## 测试

```powershell
.venv\Scripts\python.exe -m pytest -q
```
