# AI_USAGE

本文记录 RepoGate 第 7–8 步真实发生过的 AI / 规格偏差与修正，不是模型评测报告，也不声称「AI 全程零错误」。第 7 步的 schema 回归使用本地 OpenAI 兼容桩；第 8 步已用真实 DeepSeek `deepseek-flash` 跑通 `/api/advice`，两者在文中分开标注，不把桩输出包装成真实供应商结论。

## 1. 模型建议「删除 .env」，但它是 full-stack 模板的启动文件

**场景**

对 `fastapi/full-stack-fastapi-template` 做接入审查时，R09 报出 `.env` / `frontend/.env` 入库，但值都是占位。模型给出的第一条动作是删除 `.env`，理由只引用了 R09。

**原始输出**

```json
{
  "title": "删除 .env 文件",
  "why": "R09 检测到 .env 入库",
  "how": "直接删除该文件并重新提交",
  "effort": "S",
  "rule_ids": ["R09"]
}
```

**错在哪**

`.env` 是 full-stack 模板的一部分，docker compose / 启动流程依赖它；直接删除不是「修安全」，而是让项目起不来。模型只看到「R09 + .env」，没有区分「真实凭据泄漏」和「模板占位配置」，把文件存在本身当成必须删除的理由。这个建议可执行，但不可用。

**怎么改的**

- 在 `SYSTEM_PROMPT` 增加硬约束：不得建议删除项目启动所必需的模板文件；模板 `.env` 应保留，只处理真实凭据，并补 `.env.example` / `.gitignore`。
- 把 fallback 的 R09 文案从「移出版本库」改为「先确认是否模板占位；保留启动所需模板，轮换真实值并加入 .gitignore」，避免规则模板继续给出同样危险的默认动作。
- 在 `validate_model_output` 增加服务端兜底：R09 动作文本同时出现「删除」和 `.env` / 「该文件」时，整条动作丢弃；丢弃后为空则走 fallback。
- 新增 `test_delete_env_action_is_rejected`，确保以后修改 prompt 也不会把这条建议放回来。

**留下的机制**：prompt 约束 + 文案拦截 + 回归测试三层；模型说错话时，服务端有最终否决权。

## 2. 模型写了不存在的规则 R99，schema 把整条动作丢掉

**场景**

为了验证「模型不许编」，用一条模型候选输出构造回归：一条动作引用真实规则 R09，另一条引用不存在的 R99。输入允许集合只有 `{"R09"}`。

**原始输出**

```json
{
  "summary": "建议",
  "actions": [
    {
      "title": "合法动作",
      "why": "R09 检测到风险",
      "how": "处理 .env",
      "effort": "S",
      "rule_ids": ["R09"]
    },
    {
      "title": "非法动作",
      "why": "R99 不存在",
      "how": "编造",
      "effort": "M",
      "rule_ids": ["R99"]
    }
  ],
  "caveats": []
}
```

**错在哪**

`R99` 不在 14 条规则表里，也不在本次 `checks` 的 fail/degraded 集合里。如果服务端只做 JSON 解析，模型就能用不存在的规则解释一条动作；页面看起来像有依据，实际是编的。

**怎么改的**

- `validate_model_output` 对每个 action 的 `rule_ids` 做集合校验；任何未知/非 fail 的 id 都让整条动作丢弃，而不是只删 id。
- 过滤后 `actions` 为空时抛 `AdviceValidationError`，`build_advice` 捕获后走规则 fallback，HTTP 仍为 200。
- 回归断言 `test_unknown_rule_action_is_dropped`：合法动作保留，非法动作消失；`test_all_actions_unknown_falls_back`：全非法时 fallback。
- 这次回归还暴露了一个实现细节：不能只校验 `rule_ids` 的字段存在，还要校验它属于**本次**规则集合；否则模型可以引用另一条仓库的 R09 来「借依据」。

**留下的机制**：`rule_ids` 白名单集合校验；未知 id 不是「删字段」，而是整条动作丢弃。

## 3. 「remaining=0 时缓存命中不 stale」不可达成：执行方反驳，审定方修正标准

**场景**

第 7 步前置收口要求：GitHub 匿名配额 `remaining=0` 时，任一已缓存仓库仍返回 `cache.hit=true`，且 `stale=false`。原实现的 HEAD 走 `api.github.com/repos/{owner}/{repo}/commits/HEAD`，HEAD 自己也要消耗 API 配额。

**原始输出 / 原验收标准**

```text
remaining=0 时任一已缓存仓库，仍返回 cache.hit=true（不 stale）。
```

**错在哪**

这不是实现能单方面绕过的缺陷：配额已经为 0，HEAD 请求本身就会 403/429；此时只能回落到旧缓存，`cache.stale=true` 是正确行为。把「网络不可达/配额为 0」当成「缓存逻辑失败」，标准不可达成；继续按原标准验收，只会逼着执行方伪造 `hit=true` 或把 stale 藏掉，反而破坏第 6 步已经验证过的限流兜底。

**怎么改的**

- 执行方反驳：先把 HEAD 改成不消耗 API 配额的路径，再谈 `remaining=0` 的缓存验收；否则标准与 GitHub 配额事实冲突。
- 审定方修正：HEAD 改走 `https://github.com/{owner}/{repo}/commits/{branch}.atom`；缓存报告里保存 `repo.default_branch`，缓存命中只读分支名再请求 atom，不请求 API 元信息；atom 404 才回落一次元信息。
- 验收拆成两层：atom 解析 / 404 回落必须有单元测试；`remaining=0` 的真实复跑必须贴 `cache` JSON，网络不可达时记录阻塞，不伪造通过。
- 修正后真实复跑：`remaining=0` 的 octocat 请求返回 `{"hit":true,"stale":false,"cached_at":"2026-11-27T02:34:23.362621+08:00"}`；同一路径的 `/api/advice` 也能在无模型时返回 200 fallback。

**留下的机制**：把「额度」和「缓存」拆开验收；标准不可达成时，先改协议（HEAD 走 atom），再改验收。

## 4. 盘符正则 `(?i)` 自相矛盾：改为不加 `(?i)`、前瞻只排 `ntr`

**场景**

R08 要识别 Windows 硬编码路径，原始正则写成 `(?i)[A-Z]:\\...`。`(?i)` 让 `[A-Z]` 同时匹配小写字母，于是 `c:\` 也被当作盘符；但同一正则的前瞻又只排除了 `n/t/r` 三种转义，两者对「大小写」和「转义前缀」的假设互相矛盾。测试里出现「`c:\` 该不该命中」的摇摆。

**原始输出 / 原始写法**

```python
DRIVE_RE = re.compile(r'(?i)[A-Z]:\\(?!(?:...))...')
```

**错在哪**

- `(?i)` 与 `[A-Z]` 的显式范围重复，读者无法判断设计意图是「只认大写盘符」还是「大小写都认」。
- 转义前瞻只排 `n/t/r`，是为了避免把 `\n`、`\t`、`\r` 当成路径分隔符；但如果正则本身不区分大小写，`C:\temp` 和 `c:\temp` 的判定又不一致。
- 这类「靠一个 flag 同时解决两件事」的写法，改一处会悄悄改变另一处语义，容易在 golden 里制造假回归。

**怎么改的**

- 去掉 `(?i)`，盘符显式写成 `[A-Za-z]`，让「大小写都支持」变成可读的字符类，而不是隐式 flag。
- 前瞻只保留 `(?!n|t|r)`，注释写清「只排除三种转义前缀，避免误伤 `C:\foo`」。
- 增加小写 `c:\` 与 `C:\` 的对照用例；但测试只证明这两个样例，不证明全量覆盖。已知限制写成「小写已在 3.2 修复；以转义字符开头的路径（如 `s:\next`）仍可能漏」，不拿「测试通过」冒充「全仓正确」。

**留下的机制**：正则里的每个 flag 只承担一个语义；对照用例固定住「大小写」和「转义前缀」两个维度。

## 5. 缓存命中要「免配额」：从 API HEAD 改为 atom，随后又遇到 atom 不可达

**场景**

第 7 步要求缓存命中不消耗 GitHub API 配额。最初 HEAD 走 API，命中也要 1 次请求；改成 `github.com/{owner}/{repo}/commits/{branch}.atom` 后，正常网络下命中确实 0 次 API。

**原始输出 / 原始路径**

```text
HEAD = GET https://api.github.com/repos/{owner}/{repo}/commits/{branch}
```

**错在哪**

- 「缓存命中免配额」和「每次先解析 HEAD」在 API HEAD 方案下直接冲突；不识别这个冲突，就会把「缓存命中」做成「缓存 + 1 次 API」。
- 第 8 步重跑六仓时，当前网络对 `github.com` 的 atom 直连 `ConnectTimeout`（约 20 秒），`api.github.com` 可达；只重试 atom 会让真实扫描返回 502 `FETCH_FAILED`。
- 这不是模型错误，是「协议选择依赖网络现实」的教训：免配额路径也可能不可达，必须准备 API commits 兜底，并接受它会计入配额。

**怎么改的**

- 正常路径：atom HEAD；404/解析失败 → 元信息拿默认分支 → 再试 atom。
- 网络失败兜底：atom 不可达时，`repos/{owner}/{repo}/commits/{branch}` 取 HEAD，再拉 tree；`MAX_API_REQUESTS` 从 2 调到 3，并明确「正常 atom 可用时仍是 meta+tree 两次」。
- 新增 `test_atom_network_failure_falls_back_to_api_commits`；六仓 3.2 实测 `request_count=3`，在 README 已知限制里如实写出，不宣称「缓存命中永远 0 API」。
- 真实模型侧同样做了「协议兜底」：DeepSeek V4 默认把 600 token 全用在 `reasoning_content`，`content` 为空；加 `thinking:{"type":"disabled"}` 后 content 才稳定返回 JSON，失败仍回退规则并标 `fallback_reason`。

**留下的机制**：免配额是目标，不是教条；每条网络路径都要有可观测的 fallback，并在报告里暴露真实请求次数。

## 这套流程留下了什么

不是「AI 一次写对」，而是四层可回归的约束：

1. **schema 校验**：模型输出必须过字段、长度、`rule_ids` 白名单；未知规则整条丢弃，actions 为空则 fallback。
2. **fallback 标注**：无密钥、超时、坏 JSON、schema 不过都返回 HTTP 200，但 `source="rules_fallback"`、`fallback_reason` 明确；页面不冒充 AI。
3. **门槛表**：五维权重、severity 扣分、封顶上限、A/B/C/D 只在 `app/scoring.py` 定义一次；不靠调权重凑等级。
4. **golden 回归**：评分纯函数 8 组 + 六仓 3.2 快照；同一 commit 字节级复用，规则改动必须让 golden 说话。

这套流程不是让 AI「更听话」，而是让 AI 的错误在进入用户视野之前被挡住，并且每次挡住都留下测试或字段。
