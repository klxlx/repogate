# RepoGate

输入一个公开 GitHub 仓库地址，回答最基础的问题：这个仓库能不能进内网。只读当前快照，不 clone、不执行代码、不扫 Git 历史；14 条规则合成 0–100 分、A/B/C/D 等级、一句话裁决，并输出一页三卡报告。当前完成**第 6 步（v3.1）：commit 缓存 + 更新跟踪 + 契约固化 + golden 回归**。

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
| `GET /api/inspect?repo=<url>&refresh=0 或 1` | 查询参数版；每次先解析默认分支 HEAD，再决定缓存命中或重扫。`refresh=1` 跳过缓存。 |
| `POST /api/inspect` | JSON 请求体 `{"repo_url": "https://github.com/owner/repo"}`，兼容 `url` 字段；同样支持 `?refresh=1`。 |
| `GET /api/health` | 返回 `engine_version`、`schema_version`、`rules_count` |
| `GET /api/rules` | 规则注册表：`id / dimension / severity / points / needs_content` |
| `GET /docs` / `GET /redoc` | FastAPI 自动文档；两个 `/api/inspect` 方法都有 `response_model=InspectResponse` |
| `GET /healthz` | 极简存活检查 `{"ok": true}` |
| `GET /` | 单页表单，提交后展示结论 / 证据 / 建议三卡报告 |

### v3.1 顶层契约（`schema_version: 1`）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `schema_version` | int | 固定 `1`；缓存文件与响应共用 |
| `repo` | object | `owner / name / default_branch / head_sha / pushed_at / archived`；另保留 `full_name / stars / language / license` 兼容字段 |
| `commit` | object | `sha / message / author / date`；缓存报告保留原 sha |
| `scan` | object | `engine_version / scanned_at / duration_ms` |
| `stats` | object | 取数与抽样统计，字段见下文 |
| `checks` | array | 14 条规则，每条 `rule_id / status / basis / file / line` |
| `findings` | array | 命中证据；完整密钥只以掩码形式出现 |
| `score` | object | `total / raw_total / grade / verdict / caps / dimensions / deductions` |
| `cache` | object | `hit / stale / cached_at`；新扫 `hit=false, stale=false`，命中 `hit=true`，陈旧 `stale=true` |
| `warnings` | array | 结构化警告；陈旧缓存时为 `[{"code":"STALE_CACHE","message":"..."}]` |
| `error` | object/null | 成功为 `null`；失败为 `{code,message,hint,retry_after}` |
| 兼容字段 | — | `ok / provider / request_count / source / mode / ref / commit_sha / scope / timing / files / engine_version / scanned_at` 保留，页面逐步改用新字段 |

成功响应骨架：

```json
{
  "schema_version": 1,
  "ok": true,
  "repo": {"owner": "psf", "name": "requests", "default_branch": "main", "head_sha": "3c7baaa05ab4aa49cc819ce3a819d260de19c157", "pushed_at": "2026-11-02T16:55:12Z", "archived": false},
  "commit": {"sha": "3c7baaa05ab4aa49cc819ce3a819d260de19c157", "message": "…", "author": "…", "date": "…"},
  "scan": {"engine_version": "repogate-3.1", "scanned_at": "2026-11-26T17:52:10.047040+00:00", "duration_ms": 7210},
  "stats": {"secret_scan_files": 120, "secret_sampling_ratio": 0.9756},
  "checks": [{"rule_id": "R01", "status": "pass", "basis": "根目录 README.md", "file": "README.md", "line": 0}],
  "findings": [{"rule_id": "R04", "severity": "low", "file": "pyproject.toml", "evidence_masked": "…"}],
  "score": {"total": 95, "raw_total": 95, "grade": "A", "verdict": "可直接接入", "caps": [], "dimensions": [], "deductions": []},
  "cache": {"hit": false, "stale": false, "cached_at": "2026-11-26T17:52:17.000000+00:00"},
  "warnings": [],
  "error": null
}
```

`score` 单条结构（冻结）：

```json
{
  "total": 79,
  "raw_total": 90,
  "grade": "B",
  "verdict": "小修后接入",
  "caps": [{"type": "env_committed_placeholder", "limit": 79, "reason": "R09 检测到 .env 入库（.env，值为占位或空），总分上限 79"}],
  "dimensions": [
    {"key": "secrets", "label": "密钥与敏感信息", "weight": 30, "deduction": 10, "score": 20},
    {"key": "portability", "label": "平台可移植", "weight": 25, "deduction": 0, "score": 25},
    {"key": "docs", "label": "文档完备", "weight": 20, "deduction": 0, "score": 20},
    {"key": "deps", "label": "依赖与入口", "weight": 15, "deduction": 0, "score": 15},
    {"key": "maintenance", "label": "维护状态", "weight": 10, "deduction": 0, "score": 10}
  ],
  "deductions": [{"rule_id": "R09", "points": -10, "reason": ".env 随代码入库但密钥类值均为空或占位", "file": ".env"}]
}
```

- `raw_total` 是封顶前的五维加总；`total = min(raw_total, 最严 cap.limit)`。`caps` 列出所有触发的上限，`total` 取最小。
- `deductions` 只列真正扣分的 finding（info 不列）；`points` 为负分，页面按维度分组展示。
- `grade` 与 `verdict` 由封顶后的 `total` 决定，不允许出现「总分 79 却显示 A」。
- `scan.engine_version` 与 `scan.scanned_at` 为页面页脚透传字段；`scan.scanned_at` 同时是 R13 判断「距今 > 12 个月」的时间基准。顶层 `engine_version` / `scanned_at` 为第 5 步兼容字段，值与 `scan` 相同。
- `cache.cached_at` 是缓存文件的写入时间；`cache.hit` / `cache.stale` / `cached_at` 同源，页面与导出 JSON 均包含。

`findings` 单条字段（最终 schema，后续步骤不再改）：


```json
{
  "id": "R03-deps-missing",
  "rule_id": "R03",
  "category": "deps",
  "dimension": "deps",
  "severity": "high",
  "title": "缺少依赖清单",
  "file": "",
  "line": 0,
  "evidence_masked": "未找到以下候选清单文件（已检查根目录与一层子目录）：package.json、……",
  "impact": "无法判断依赖来源与版本，供应链风险不可评估",
  "fix": "补充标准依赖清单（requirements.txt / pyproject.toml / package.json 等）",
  "hit_count": 1,
  "confidence": "high",
  "cap_eligible": false
}
```

失败响应：`{"schema_version":1,"ok":false,"error":{"code":"…","message":"中文提示","hint":"恢复建议","retry_after":null}}`。HTTP 状态与错误码：

| HTTP | code | hint（对外恢复建议） |
| --- | --- | --- |
| 400 | `INVALID_URL` | 示例：`https://github.com/psf/requests` |
| 404 | `REPO_NOT_FOUND` | 确认仓库地址、公开状态与网络可达 |
| 403/429 | `RATE_LIMITED` | 设置 `GITHUB_TOKEN`；有缓存时返回 stale 旧报告；`retry_after` 为 `X-RateLimit-Reset` 换算的剩余秒数 |
| 502 | `FETCH_FAILED` | 检查网络与 GitHub 状态；重复失败看日志 |
| 504 | `TIMEOUT` | 稍后重试；文件树接近 90 秒硬上限 |
| 500 | `INTERNAL` | 服务端未预期错误；不要当作仓库结论 |

- 错误码只有这 6 个；`UPSTREAM_ERROR` / `INTERNAL_ERROR` / `NOT_FOUND` 为第 5 步旧名，v3.1 已统一替换。
- `error` 在成功响应中为 `null`；在 429 无缓存时 `retry_after` 为整数秒，缺失重置头时为 `null`。

## 更新跟踪设计（v3.1）

这一节回答四个问题，每条都指向代码或字段。

### 1. 怎么发现更新

**每次访问都先解析默认分支 HEAD，再与缓存 key 比对。** `_inspect` 先调用 `app/github.py::resolve_head`（单次 `commits/HEAD`，1 次轻请求），得到 `head_sha`；`app/main.py::_inspect` 用 `owner/repo@sha` 查缓存。一致就复用，不一致就重扫。没有轮询、没有 webhook，也不依赖 `pushed_at` 猜测。

### 2. 怎么避免全量重算

**commit 内容寻址。** key 为 `owner/repo@commit_sha`，文件落在：

```text
.cache/reports/<owner>__<repo>/<sha>.json
```

缓存内容是完整报告 JSON + `schema_version` + `cached_at` + `duration_ms`，由 `app/cache.py::save` 原子写入（同目录 tmp + `os.replace`），失败不落盘；读取由 `cache.load` 完成。命中时不再请求元信息、tree，也不读 raw 内容，只保留 1 次 HEAD 轻请求。每仓按 mtime 保留最近 5 份，`_prune` 淘汰第 6 份及更旧。连接池复用后，热身状态下缓存命中实测约 110–135ms（上限要求 <300ms）。

commit 不变 → 字节级复用；commit 变了 → key 自然不同，不需要任何缓存失效逻辑。`?refresh=1` 跳过命中并重新扫描。

### 3. 报告怎么标注 commit

- JSON：`commit.sha`（缓存报告保留原 sha，不把旧报告改成新 HEAD）。
- 页面页脚：完整 commit SHA、扫描时间、引擎版本、`schema_version`、缓存状态（命中 / 新扫 / 陈旧）、缓存时间。
- 缓存文件本身不覆盖 commit；同一 commit 的 score 与 checks 可逐字节复用。

### 4. 限流失败怎么兜底

`app/main.py::_inspect` 捕获 `ScanError.code == "RATE_LIMITED"`（来源 403/429）：

- **有任意缓存（哪怕旧 sha）**：`cache.load_latest` 取最新一份，返回 HTTP 200；`cache.stale=true`、`cache.hit=false`，并在 `warnings` 写入 `STALE_CACHE`；页面顶部黄色警告条显示缓存时间与原 commit，不遮报告。旧报告的 `commit.sha` 保持原值。
- **无缓存**：抛出 429；`error={code:"RATE_LIMITED",message,hint,retry_after}`，`retry_after` 由 `X-RateLimit-Reset` 换算为剩余秒数（缺失时为 `null`）。页面显示错误与恢复建议，不返回空白页，也不假装扫描成功。

限流兜底不改变规则结论，只改变缓存元数据；导出 JSON 同样含 `cache` 与 `warnings`。

**未来工作**：webhook / 定时重扫、分布式缓存、多用户隔离与数据库都不在 v3.1 范围内；当前只用本地文件缓存。

## 取数边界

- 首次扫描固定 3 次 GitHub API 请求：默认分支 HEAD commit、仓库元信息、`git/trees/{commit_sha}?recursive=1`；缓存命中只发第 1 次 HEAD。不 clone、不扫 Git 历史、不下载任何文件内容、不调用 contents API。
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
| `scope.files_scanned` | 通过过滤、真正纳入分析的文件数（文本、路径安全、单文件 ≤1MB、路径不含排除目录段） |
| `scope.files_skipped` | `listed_blobs - files_scanned`（媒体 / 二进制 / 超大 / 无效路径） |
| `scope.bytes_scanned` | `files_scanned` 的 `size` 之和（分析口径，不代表下载量） |
| `stats.bytes_total_listed` | `listed_blobs` 的 `size` 之和（清单口径） |
| `stats.unlisted_blobs` | `total_blobs_in_tree - listed_blobs`（因上限没看到的） |

- 恒等式（代码内 assert，单测覆盖）：
  - `files_scanned + files_skipped == listed_blobs`
  - `listed_blobs + stats.unlisted_blobs == stats.total_blobs_in_tree`
- `stats.text_files` 与 `files_scanned` 同口径；`stats.windows_script_count` 与 R06 同源，只统计**纳入分析范围**（排除目录段已剔除）的 `.bat/.cmd/.ps1/.vbs`。全树原始数量另由 `snapshot.raw_windows_scripts` 提供，仅用于 R06 basis 注脚，不进 stats。Windows 脚本数一律按纳入分析范围统计。
- `stats.path_scan_files` / `stats.path_scan_bytes` 为实际读取到的文本文件数与字节；`stats.content_total_text_files` 为可读文本文件总数（= `files_scanned`）。
- `stats.secret_scan_files` / `stats.secret_scan_bytes` 为密钥规则实际扫描的文件数与字节；`stats.secret_sampling_ratio = secret_scan_files / content_total_text_files`（保留 4 位小数），用于判定封顶资格与 R08/R10/R11 的抽样折半，不得隐藏。
- `files` 只返回 `files_scanned` 集合中按路径排序的前 50 条。
- 排除目录段（任意一段命中即整条跳过）：`.git` / `node_modules` / `vendor` / `dist` / `build` / `.venv` / `venv` / `__pycache__` / `.pytest_cache` / `.idea` / `.vs` / `obj`。
- 网络重试：meta 与 tree 遇 5xx / `RemoteProtocolError` 重试 2 次（退避 1s → 3s），tree 实际尝试次数记 `timing.tree_attempts`；三次都失败才报 `FETCH_FAILED`，404/403/429 与超时不重试。
- 耗时策略：`tree_ms > 45000` 时 `timing.tree_slow=true` 并打印 WARNING，页面顶部横幅提示「超大仓库，本次耗时较长」；`tree_ms > 90000` 才返回 `TIMEOUT`。不再在 60 秒处截杀。

## 规则引擎（第 2 步）

`GET /api/rules` 返回注册表，代码在 `app/rules/`：

| id | slug | dimension | severity | points | needs_content | 判定口径 |
| --- | --- | --- | --- | --- | --- | --- |
| R01 | readme-missing | docs | high | -10 | 否 | 最高可用层级无 README（接受 `.md/.markdown/.rst/.txt` 与无扩展名 `README`，大小写不敏感） |
| R02 | readme-incomplete | docs | medium | -4 | 是 | R01 未命中时，README 前 20KB 覆盖 <2 类：A 安装构建 / B 运行使用 / C 配置环境 / D 依赖前置 |
| R03 | deps-missing | deps | high | -8 | 否 | 最高可用层级无任何依赖清单（含 `.sln/.slnx` / `.csproj` / `vcpkg.json` / `CMakeLists.txt`） |
| R04 | deps-unpinned | deps | low | -2 | 是 | 无可解析清单（`package.json` / `requirements.txt` / `pyproject.toml`）时 `n/a`；有清单时无 lockfile 且浮动版本 >50% 判 fail |
| R05 | start-missing | deps | medium | -4 | 是 | 最高可用层级无 `package.json:scripts.start/dev/serve/build/compile`、`Makefile`、`Dockerfile`、`docker-compose*`、`*.sln/.slnx`；根无 `CMakeLists.txt` / `build.ps1` / `build.bat` / `build.sh` / `*.csproj` |
| R06 | windows-scripts | portability | high | -6 | 否 | 统计纳入分析范围的 `.bat/.cmd/.ps1/.vbs`（与 `stats.windows_script_count` 同源）；0 个 pass，1 个 info（扣 0，保留 finding），2–4 个 medium（-3），≥5 个 high（-6）；share = win/(win+.sh+.bash) < 0.5 时最高只判 medium；basis 可带注脚「全树 N，其中 M 个未纳入（排除目录 A 个、BLOB_CAP 未列出 B 个）」 |
| R07 | no-crossplatform | portability | medium | -4 | 否 | 无 Windows 脚本 → n/a；有 Windows 脚本且存在 `.sh/.bash/Makefile/Dockerfile/docker-compose*.yml` → pass；全无 → fail（-4） |
| R08 | hardcoded-path | portability | high | -10 | 是 | 扫 `path_scan_files` 文本，识别盘符 / UNC / `/Users|home/<user>` / `/mnt/<x>`；占位符、示例用户名、CI runner、占位路径段、文档降级 low，`tests/test/fixtures/examples/docs/samples` 目录降级 info；同文件合并，high 每文件 -2，单规则封顶 -10；basis 必含「抽样 X/Y（Z%）」，Z<20% 时 fail 记 medium confidence 并声明「仅基于抽样文件，非全仓结论」，零命中记 degraded |
| R09 | env-committed | secrets | critical | -10 | 是 | tree 命中 `.env` / `.env.*`（排除 `.env.example/.sample/.template/.dist`，大小写不敏感）；读前 4KB：密钥类 KEY 有非占位值 → critical（封顶 69），密钥类值全空/占位 → medium（-10，封顶 79）；不存在但 `.gitignore` 未包含 `.env` → low（提示）；`tests/testdata` 等目录中的 `.env` 视为夹具降级 low；evidence 只显示 `KEY=***`。密钥类 KEY 冻结表：`secret\|token\|password\|passwd\|pwd\|api[_-]?key\|access[_-]?key\|credential\|auth\|private`，按词边界匹配（`AUTHOR` 不算 `auth`） |
| R10 | private-key | secrets | critical | -15 | 是 | 内容命中 `-----BEGIN … PRIVATE KEY-----` → critical（封顶 59）；tree 命中 `.pem/.key/.p12/.pfx/.jks/id_rsa*` 但内容不可读 → high（-15）；`tests/test/fixtures/examples/docs` 及测试文件降级 low |
| R11 | secret-token | secrets | critical | -15 | 是 | 内容命中 OpenAI/Anthropic/AWS/阿里云/GitHub/Slack/Google 令牌、JWT、Bearer、带账密数据库 URL；占位值、重复字符 → low，文档 → info，测试/示例目录 → low；命中行属于规则/模式定义（路径含 `rules/` `patterns/`、文件名为 `gitleaks.toml` 或 `*rule*`，或同时含 `[ ] { } ( ) \| \ * + ?` 与 `regex/pattern/re.compile` 等关键字）→ info，不封顶；其余按封顶资格判 critical（封顶 59）或 high（-15，不封顶） |
| R12 | license-missing | maintenance | low | -4 | 否 | 根目录无 `LICENSE` / `LICENCE` / `COPYING`（允许 `.md/.txt` 后缀）且 GitHub `license` 元信息可判定 → low 提示；许可证文件存在 → pass；`license` 元信息为 `null` / 空 / `NOASSERTION` → `n/a`，不猜 |
| R13 | stale-repo | maintenance | low | -3 | 否 | `pushed_at` 距 `scanned_at` > 365 天 → low 提示；≤365 天 pass；`pushed_at` 缺失/不可解析 → `n/a`，不猜 |
| R14 | archived | maintenance | low | -3 | 否 | `archived=true` → low 提示；`false` → pass |

- 规则层是纯函数 `detect(snapshot)`，不发起任何网络请求；内容由内容层预取后放进 `Snapshot`。
- 事实型规则（缺文件）`file=""`、`line=0`，evidence 固定写「未找到 X（已检查最高层级：根目录与一层普通子目录）」。
- 同 `rule_id` + 同 `file` 合并为一条，累加 `hit_count`；`category` 当前与 `dimension` 同值。
- 所有 evidence 统一走 `app/masking.py::mask_evidence`：token/密码形态、本机绝对路径脱敏，超 240 字符截断。

### 证据层级（v1.7）

所有「找文件」的规则（R01 / R03 / R04 / R05）共用 `app/rules/evidence.py`：

| 层级 | 范围 | 说明 |
| --- | --- | --- |
| 0 级 | 根目录 | 最优，预算优先给根目录文件 |
| 1 级 | 一层普通子目录 | 次优，如 `installer/setup.py` |
| 2 级 | 弱目录：`docs` / `doc` / `tools` / `scripts` / `examples` / `test` / `tests` / `fixtures` / `.devcontainer` / `.github` | 只能判 `degraded`，不能判通过 |

- 只在当前可用的最高层级里选，禁止跨层「找到就算」；同层多个候选按路径字典序取第一个，并在 basis 注明「共 N 个候选」。
- basis 统一带层级：`根目录 package.json`、`根目录 pyproject.toml（共 3 个候选）`、`一层子目录 installer/setup.py`、`仅在弱目录 docs/requirements.txt 发现，无法确认主项目`。
- 根目录存在同类型文件时，嵌套或弱目录证据不得作为主证据。
- 超过一层（深度 ≥2）的普通路径不参与文件定位；锁文件只在清单所在层级或更上层查找，避免被 `src/Monaco/package-lock.json` 这类子包锁误导。

### checks 四态

`/api/inspect` 与页面都按规则逐条输出 `checks`，每条规则出现且只出现一次：

| status | 含义 | 与 findings 的关系 |
| --- | --- | --- |
| `pass` | 通过；允许 low / info 提示 | 不得有 medium 及以上 finding；low/info finding 保留，basis 追加「提示 N 条（-X）」；X=0 时写「仅提示 N 条」；X 由 `score.deductions` 同源计算 |
| `fail` | 未通过 | 必须至少有一条 medium / high / critical finding |
| `degraded` | 降级：依据文件读取失败或只在弱目录命中，结论不完整（页面黄色） | 部分依据可用时输出 info finding；全部依据失败时只在 checks 记 degraded，不出 finding |
| `n/a` | 不适用（页面灰色） | 不允许有对应 finding；该维度仍按固定权重计满分，不因规则不适用而放大其它维度 |

- `basis` 必须可复核，写具体文件名或路径与层级：
  - `根目录 PowerToys.slnx（Windows 专用构建入口，Linux/容器环境需替代方案）`
  - `根目录 pyproject.toml 读取失败，仅按 requirements.txt 判断，结论不完整`
  - `无可解析清单，规则不适用`
- R05 的 `package.json` 命中集合在 v1.8 收窄为 `start/dev/serve/build/compile`（不再认 `watch/test/lint`）；basis 分叉：`scripts.start` 等写「启动入口」，`scripts.build/compile` 写「构建入口，无启动命令」。
- R05 命中 Windows 专用入口（`.sln` / `.slnx` / `.csproj` / `build.ps1` / `build.bat`）时，basis 标注「Linux/容器环境需替代方案」；同一文案常量 `WINDOWS_ONLY_IMPACT` 供 R06/R07 复用。
- 一致性断言（代码内 assert，单测覆盖）：checks 条数 == 规则数（14）、`rule_id` 不重复、`fail` ⇔ 存在 medium+ finding、`pass` 不得有 medium+ finding、`n/a` 不得有 finding、`basis` 非空。

### 密钥与敏感信息（第 4 步）

- R09 / R10 / R11 的每条 finding 带 `cap_eligible`：`secret_sampling_ratio ≥ 0.5` 或命中高风险路径（`.env*`、`*.pem/*.key/*.p12/*.pfx`、`id_rsa*`、`*credential*`、`*secret*`、根目录配置文件）时为 `true`；据此封顶（R09 critical 69 / medium 79，R10 / R11 59）。
- 抽样率 <20% 时，R08 / R10 / R11 的 finding 一律 `cap_eligible=false`，扣分减半；不允许用低抽样触发封顶。
- 不满足封顶资格时仍然出 finding，但 `severity=high`、`cap_eligible=false`，impact 固定写「抽样范围内发现，未构成全仓结论」。
- 掩码：`app/masking.py::mask_secret` 统一为「前 4 + *** + 后 2」，长度 <8 全 `***`。R09 evidence 只显示 `KEY=***`；完整密钥不得出现在响应 JSON、页面、日志或 golden 文件。
- 占位判定（R11）：值含 `xxx / your / example / sample / demo / test / dummy / fake / placeholder / changeme / changethis / redacted / < / ${ / {{ / %s`，或去掉前缀后由同一字符重复构成 → low，不封顶。
- 内容预取把 `.env*`、`.gitignore`、`.pem/.key/id_rsa*`、`*credential*/*secret*` 前移到普通抽样之前，保证 R09/R10 尽量拿到原文。

## 评分模型（v3.1：权重与门槛冻结）

评分口径是产品主张，公开接受质疑；代码在 `app/scoring.py`，所有数字只在这里定义一次。

### 维度与权重

| key | 维度 | 权重 |
| --- | --- | --- |
| `secrets` | 密钥与敏感信息 | 30 |
| `portability` | 平台可移植 | 25 |
| `docs` | 文档完备 | 20 |
| `deps` | 依赖与入口 | 15 |
| `maintenance` | 维护状态 | 10 |

`维度分 = max(0, 权重 − 该维度所有 finding 的扣分合计)`；`raw_total = 五维相加`（满分 100）。

### 扣分映射与规则-分值总表（14 条逐条）

`/api/rules` 的 `points` 是规则标称值；`score.deductions` 才是实际扣分。下表把两者同时列出，避免“旧规则分”这类模糊表述。

| 规则 | 维度 | 标称 points | 评分实际扣分（每个 finding） |
| --- | --- | ---: | --- |
| R01 | docs | -10 | -10 |
| R02 | docs | -4 | -4 |
| R03 | deps | -8 | -8 |
| R04 | deps | -2 | -2 |
| R05 | deps | -4 | -4 |
| R06 | portability | -6 | -6 |
| R07 | portability | -4 | -4 |
| R08 | portability | -10 | 每个 `high` 文件 -2，`low/info` 0，单规则最多 -10 |
| R09 | secrets | -10 | 按 severity：`critical -30 / medium -10 / low -3 / info 0` |
| R10 | secrets | -15 | 按 severity：`critical -30 / high -15 / low -3 / info 0` |
| R11 | secrets | -15 | 按 severity：`critical -30 / high -15 / low -3 / info 0` |
| R12 | maintenance | -4 | 按 `low` 实扣 -3（标称 -4 只出现在 `/api/rules`） |
| R13 | maintenance | -3 | 按 `low` 实扣 -3 |
| R14 | maintenance | -3 | 按 `low` 实扣 -3 |

R09–R14 由 finding 的 `severity` 决定，不由规则各自随意定；R01–R08 使用固定规则分。唯一来源是 `app/scoring.py` 的 `LEGACY_POINTS` / `SEVERITY_POINTS`，页面与 `score.deductions` 不会另算一套。

| severity | 扣分 |
| --- | --- |
| `critical` | -30 |
| `high` | -15 |
| `medium` | -10 |
| `low` | -3 |
| `info` | 0 |

例外：R09 / R10 / R11 命中测试/样例目录（`tests/` `test/` `fixtures/` `examples/` `docs/` `samples/` `testdata/` 等）且 severity 为 `low` 时按 0 分处理，只提示、不扣分——这些通常是假密钥夹具。

### 抽样折半

- R08 / R10 / R11 的扣分，在 `stats.secret_sampling_ratio < 20%` 时减半（四舍五入到整数，最少 1 分）。
- 同一条件下这些规则的 finding 一律 `cap_eligible=false`，不允许用低抽样触发封顶。
- R09 不受折半影响：`.env` 的判定只看文件是否存在与前 4KB 内容，不依赖大范围抽样。

### 封顶（按顺序取最严）

| 触发条件 | 上限 | 等级 |
| --- | --- | --- |
| R10 / R11 存在 `critical` 且 `cap_eligible=true` | 59 | D |
| R09 存在 `critical`（真实值入库） | 69 | C |
| R09 存在 `medium`（占位值入库 / `.env` 被提交） | 79 | B |
| `docs + deps` 维度扣分合计 ≥ 12 | 59 | D |
| `docs + deps` 维度扣分合计 8–11 | 69 | C |

- **R07 不设门槛**：委托方本来就跑 Windows，缺少跨平台入口是迁移成本，不是阻断项。这条是明确的产品判断，不是遗漏。
- 多个 cap 同时触发时，`caps` 全部列出，`total` 取最小上限。
- `total = min(raw_total, 最严 cap.limit)`；`grade` 与 `verdict` 只看封顶后的 `total`。

### 等级与裁决

| 总分 | 等级 | 裁决 |
| --- | --- | --- |
| ≥ 85 | A | 可直接接入 |
| 70–84 | B | 小修后接入 |
| 60–69 | C | 需整改 |
| < 60 | D | 不建议接入 |

### 报告页（一页三卡）

- 卡 1 结论：纯 CSS `conic-gradient` 分数环（无图表库）、等级、裁决、阻断项数（critical finding 数）、高危数（high finding 数）、内容抽样率；门槛区显示「已检查 / 已生效 / 未生效（原始分更低）」。
- 卡 2 证据：五维横向条（得分/权重，点开可见该维度每一笔 `deductions`）、14 条检查矩阵（通过 / 提示 / 未通过 / 不适用 四色，悬停显示 basis）、风险表（critical → info 排序，含 rule_id、文件:行号、掩码证据、影响、修复建议；medium 以下默认折叠）。
- 卡 3 建议：本步只放占位「AI 建议将在后续接入」，不伪造 AI 结论。
- 页脚：commit SHA、扫描时间、引擎版本、`schema_version`、缓存状态（命中 / 新扫 / 陈旧）、缓存时间、文件数、内容抽样率、封顶原因、导出 JSON 按钮。导出内容来自同一 `InspectResponse`，与 `/api/inspect` 完全一致（含 `cache`）。
- 陈旧缓存：顶部黄色警告条显示「上游限流，以下为 T 时间的缓存结果（commit Y），可能已过期」，报告不隐藏。
- 375px 不横向溢出：三卡单列、风险表转为纵向卡片、矩阵单列；桌面为双列矩阵与标准表格。





- 默认匿名调用：60 次/时（GitHub 按来源 IP 计）。首次扫描固定 3 次 API 请求（HEAD + 元信息 + tree），缓存命中只发 1 次 HEAD；满配额约可覆盖 20 次首次扫描/时。
- 可选：设置环境变量 `GITHUB_TOKEN`（公开仓库只读即可，无需额外 scope），配额提升到 5000 次/时。每次扫描读取一次，只放进 `Authorization: Bearer` 请求头；不写进代码、不出现在响应、不出现在日志。
- 403/429 时读取 `X-RateLimit-Reset`（epoch 秒）并转为本地时间，返回：`GitHub 配额已耗尽，预计 HH:MM 恢复；本地可设置 GITHUB_TOKEN 提升到 5000 次/时`。响应头缺失时退回原通用提示。
- 日志只记录请求方法与 URL，不记录请求头。

## 内容读取层

- 只读 `https://raw.githubusercontent.com/{owner}/{repo}/{commit_sha}/{path}`，不调用 contents API。
- 配额：≤120 个文件、≤8MB 总量、并发 6、单文件超时 15s、单文件 ≤512KB；超时与 5xx 各重试 1 次，404 不重试。预取顺序按证据层级：README 取最高层，清单取最高层同层全部，弱目录不取，预算先给根目录文件；随后是 `.env*` / `.gitignore` / 可读私钥等密钥候选；其余文本按 P1–P5（配置 → 脚本 → 说明/清单 → 源码 → 其他）排序补齐。
- 抽样比例：`stats.path_scan_files / stats.content_total_text_files`，报告与页面不得隐藏；`path_scan_bytes` 为实际读取字节。R08 的 basis 必须写明「抽样 X/Y（Z%）」；Z<20% 时不允许给出干净结论：有命中记 fail + `confidence=medium`，impact 追加「仅基于抽样文件，非全仓结论」，零命中记 degraded「抽样率过低（Z%），未发现不代表不存在」。
- R08 豁免分层：路径位于 `tests/` `test/` `fixtures/` `examples/` `docs/` `samples/`（任意层级同名目录）或 `*_test.go` / `test_*.py` / `*.spec.ts` 等测试文件 → info；路径段精确命中 `foo` `bar` `baz` `example` `sample` `demo` `test` `your-path` `path-to` → low；用户名命中占位表（含 `someone` `alice` `bob` `jane` `john` `doe` `dev` `developer` `test`）→ low。任一条生效时 `confidence=medium`，evidence 保留原值不掩码路径。
- 请求失败、超时、超配额：该文件标为不可用；依赖内容的规则**不得输出肯定结论**，一律 `degraded` + `confidence=low`，说明「缺 X 文件内容，结论不完整」。
- `stats.content_requests` / `stats.content_bytes` / `stats.content_failed` / `stats.content_files_available` 单独统计 raw 拉取；`request_count` 仍只算 api.github.com。

## 已知限制

- Starlette 1.10.0 对 `httpx` TestClient 的升级提示只出现在 pytest；生产路径不用 TestClient，`pytest.ini` 与 `tests/conftest.py` 按依赖版本原因过滤，不修改业务代码。
- 429 两分支已用真实耗尽匿名配额复现：有缓存返回 200 + `stale` + `STALE_CACHE`，无缓存返回 429 + `retry_after`；本机未设置 `GITHUB_TOKEN` 时仍建议先等配额重置再批量扫描。
- 匿名配额 60 次/时；首次扫描 3 次 API 请求，缓存命中 1 次 HEAD，约 20 个首次扫描/时；建议本地设置 `GITHUB_TOKEN` 提升到 5000 次/时。
- 密钥规则受抽样率约束：抽样 <50% 且命中文件不在高风险路径时只作 high 提示，不封顶，不能据此断言全仓无泄露。
- R09 只解析 `.env` 前 4KB 的 `KEY=VALUE`；多行值、注释中的值不参与判定。
- 占位词表是启发式，宁可漏报也不误报：真实值若恰好含 `test` / `example` 等词会被降级为 low。
- 测试/样例目录（`tests/` `test/` `fixtures/` `examples/` `docs/` `samples/` `testdata/` 等）里的敏感命中只提示、不扣分（按 low / info 计），避免把假密钥夹具当成事故。
- R11 的「规则定义降级」不是只看 `[ ] { } ( ) | \ * + ?`：普通配置字典（如 `{"Authorization": "Bearer …"}`）也含 `{}`，若无条件降级会把真实配置误判成规则样例。实现收紧为「规则路径，或同时含元字符与 `regex/pattern/re.compile` 等关键字」；这是对字面口径的主动收窄，需审定。
- R12/R13/R14 只读仓库元信息与根目录文件名；`pushed_at` 缺失或 `license` 元信息缺失时 R13/R12 记 `n/a`，不猜测。

## Golden 回归

### A. 评分纯函数（离线、默认必跑）

固定 findings → 期望 `total / raw_total / grade / caps / deductions`，数据在 `tests/golden/scoring_cases.json`，测试在 `tests/test_scoring_golden.py`。当前 8 组：

1. 无 finding（100 A）
2. 单 high（R06）
3. R09 critical（69 C）
4. R10 + R11 两 critical（59 D）
5. R08 抽样折半
6. docs+deps=8（69 C）
7. docs+deps=12（59 D）
8. R09 medium（79 B）

```powershell
.venv\Scripts\python.exe -m pytest tests/test_scoring_golden.py -q
```

### B. 六仓 summary（需联网，手动更新）

`tests/golden/six_repos.json` 只存稳定字段：`repo / commit / score.total / grade / checks（每规则 status） / finding_counts（每规则条数）`；不存扫描时间、耗时、扫描顺序、evidence。`scripts/regen_golden.py` 手动刷新，不放进默认 CI；测试在文件存在且设置 `REPOGATE_GOLDEN_NETWORK=1` 时联网比对，否则 skip。

```powershell
.venv\Scripts\python.exe scripts/regen_golden.py
$env:REPOGATE_GOLDEN_NETWORK="1"; .venv\Scripts\python.exe -m pytest tests/test_six_repos_golden.py -q
```

配额不足时不更新 golden；`refresh=1` 会跳过本地缓存，确保拿到新 HEAD。

## 当前明确不做

AI 建议卡（第 7 步）、webhook / 定时重扫、分布式缓存、多用户隔离、数据库、内容全量下载、登录、历史与组织看板、前端框架、私有仓库 / GitLab / Gitee、CVE 漏洞库、Git 历史扫描、密钥熵检测、通用 `password=` 规则、gitleaks 二进制集成；页面除一条耗时横幅与第 6 步指定的页脚字段外不展示其它 `stats/timing`，验收以 `/api/inspect` JSON 为准。

## 测试

```powershell
.venv\Scripts\python.exe -m pytest -q
```
