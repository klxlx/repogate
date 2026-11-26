# RepoGate

输入一个公开 GitHub 仓库地址，先回答最基础的问题：这个仓库真实长什么样、基于哪个 commit、我们看见了哪些文件。当前完成**第 1 步（收尾 v1.3）：git/trees 元数据清单 + BLOB_CAP 采样**，尚未实现规则、评分与报告页。

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
  "scope": {"files_scanned": 130, "files_skipped": 0, "bytes_scanned": 4451562, "truncated": false, "truncate_reason": "none"},
  "stats": {"text_files": 124, "windows_script_count": 1, "total_blobs_in_tree": 130, "listed_blobs": 130},
  "timing": {"meta_ms": 1070, "commit_ms": 441, "tree_ms": 431},
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
- `scope.bytes_scanned` 是**元数据口径**：清单内计入 blob 的 `size` 之和，不代表下载量；清单阶段不读取任何文件内容。
- `stats.total_blobs_in_tree` 是树内 blob 总数；`stats.listed_blobs` 是计入清单的 blob 数，两者相等说明清单完整。
- `stats.text_files` 是计入清单的 blob 中按扩展名判为文本的数量（含大于 1MB 的文本）；`stats.windows_script_count` 是全仓库 blob 中 `.bat/.cmd/.ps1/.vbs` 的实际数量，不受清单限额影响。
- `scope.files_scanned` 与 `listed_blobs` 同值；`scope.files_skipped` 只统计因 `BLOB_CAP` 未列入的数量，不把媒体/二进制过滤混进该字段。
- `files` 展示层仍跳过 `.git` / `node_modules` / `vendor` / `dist` / `build` / `.venv`、二进制、图片视频、压缩包与单文件大于 1MB，因此展示条数会少于 `listed_blobs`。
- 耗时保护：`tree_ms > 50000` 打印 WARNING；`tree_ms > 60000` 返回 `TIMEOUT`，不静默拖死。

## 第 1 步明确不做

规则检测、评分、三卡报告、内容抽样/下载、缓存、数据库、登录、前端框架、私有仓库 / GitLab / Gitee、CVE 漏洞库、Git 历史扫描；页面不展示 `stats/timing`，验收以 `/api/inspect` JSON 为准。

## 测试

```powershell
.venv\Scripts\python.exe -m pytest -q
```
