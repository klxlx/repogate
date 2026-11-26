# RepoGate

输入一个公开 GitHub 仓库地址，先回答最基础的问题：这个仓库真实长什么样、基于哪个 commit、我们看见了哪些文件、跳过了什么。当前完成**第 1 步（修订版）：用 git/trees 取元数据清单**，尚未实现规则、评分与报告页。

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
  "scope": {"files_scanned": 123, "files_skipped": 7, "bytes_scanned": 664038, "truncated": false, "truncate_reason": null},
  "stats": {"text_files": 123, "windows_script_count": 1},
  "timing": {"meta_ms": 753, "commit_ms": 564, "tree_ms": 833},
  "files": [{"path": ".coveragerc", "size": 33, "is_text": true}]
}
```

失败响应：`{"ok": false, "error": {"code": "INVALID_URL", "message": "中文提示"}}`，HTTP 状态与错误码对应：`INVALID_URL` 400、`NOT_FOUND` 404、`RATE_LIMITED` 429、`TIMEOUT` 504、`FETCH_FAILED` / `UPSTREAM_ERROR` 502。

## 取数边界

- 单仓库固定 3 次 GitHub API 请求：仓库元信息、默认分支 commit、`git/trees/{commit_sha}?recursive=1`。不 clone、不扫 Git 历史、不下载任何文件内容、不调用 contents API。
- trees 按 commit SHA 请求；仅当 SHA 返回 404/422 时回退默认分支重试一次，`source.tree_ref_fallback=true`，这是唯一允许的第 4 次请求。
- 不落盘、不解析 zip；项目目录不保留任何压缩包或解压产物。
- 只统计 `type=blob`；跳过 `.git` / `node_modules` / `vendor` / `dist` / `build` / `.venv`，二进制、图片视频、压缩包，单文件大于 1MB。
- 限额：2000 文件 / 50MB。`scope.truncated=true` **只表示**清单被我们自己的限额截断，并写清原因；任何取数失败、解析失败、超时都返回错误码，不出报告。
- GitHub 自己返回的 `truncated` 单独透出到 `source.tree_truncated_by_github`，不与 `scope.truncated` 混用。
- `stats.text_files` 统计本次清单内的文本文件；`stats.windows_script_count` 统计全仓库 blob 中 `.bat/.cmd/.ps1/.vbs` 的实际数量，不受清单限额影响，仅用于验收。
- `files` 最多 50 条，按路径字典序排序。

## 第 1 步明确不做

规则检测、评分、三卡报告、内容抽样/下载、缓存、数据库、登录、前端框架、私有仓库 / GitLab / Gitee、CVE 漏洞库、Git 历史扫描。

## 测试

```powershell
.venv\Scripts\python.exe -m pytest -q
```
