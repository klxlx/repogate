# RepoGate

输入一个公开 GitHub 仓库地址，先回答最基础的问题：这个仓库真实长什么样、基于哪个 commit、我们看见了哪些文件、跳过了什么。当前完成**第 1 步：取数与文件清单**，尚未实现规则、评分与报告页。

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

成功响应结构：

```json
{
  "ok": true,
  "repo": {"full_name": "owner/repo", "stars": 0, "language": null, "license": null, "archived": false, "pushed_at": null},
  "ref": "main",
  "commit_sha": "40 位 SHA",
  "scope": {"files_scanned": 0, "files_skipped": 0, "bytes_scanned": 0, "truncated": false, "truncate_reason": null},
  "files": [{"path": "README.md", "size": 123, "is_text": true}]
}
```

失败响应：`{"ok": false, "error": {"code": "INVALID_URL", "message": "中文提示"}}`，HTTP 状态与错误码对应：`INVALID_URL` 400、`NOT_FOUND` 404、`RATE_LIMITED` 429、`TIMEOUT` 504、`UPSTREAM_ERROR` 502。

## 取数边界

- 单仓库固定 3 次 GitHub API 请求：仓库元信息、默认分支 commit、`zipball/{commit_sha}`；不 clone、不扫 Git 历史、不逐文件调用 contents API。
- zipball 在内存中用 `zipfile` 解析，不落盘；项目目录不保留 zip 或解压产物。
- 跳过：`.git` / `node_modules` / `vendor` / `dist` / `build` / `.venv`，二进制、图片视频、压缩包，单文件大于 1MB。
- 限额：解压总量 50MB、文件 2000 个；超限停止并返回 `truncated=true` 与原因。
- 下载保护：压缩包超过 128MB 或下载超过 180 秒即停止，按 `truncated=true` 返回快照不完整，不返回 500。
- 文件清单最多 50 条：根级文件优先全列，其余按顶层目录轮转抽样，同一 commit 输出顺序恒定。

## 第 1 步明确不做

规则检测、评分、三卡报告、数据库、登录、前端框架、私有仓库 / GitLab / Gitee、CVE 漏洞库、Git 历史扫描。

## 测试

```powershell
.venv\Scripts\python.exe -m pytest -q
```
