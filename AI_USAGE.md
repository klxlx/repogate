# AI_USAGE

本文记录 RepoGate 第 7 步（v3.2）真实发生过的 AI 使用偏差与修正，不是模型评测报告，也不声称“AI 全程零错误”。

本步没有可用的外部模型密钥；模型路径用本地 OpenAI 兼容桩服务返回固定候选 JSON 验证。下面的 `.env` 偏差来自 full-stack 仓库复核记录，schema 与验收记录来自本步实际回归，不把桩输出包装成真实供应商结论。

## 1. 模型建议“删除 .env”，但它是 full-stack 模板的启动文件

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

`.env` 是 full-stack 模板的一部分，docker compose / 启动流程依赖它；直接删除不是“修安全”，而是让项目起不来。模型只看到“R09 + .env”，没有区分“真实凭据泄漏”和“模板占位配置”，把文件存在本身当成必须删除的理由。这个建议可执行，但不可用。

**怎么改的**

- 在 `SYSTEM_PROMPT` 增加硬约束：不得建议删除项目启动所必需的模板文件；模板 `.env` 应保留，只处理真实凭据，并补 `.env.example` / `.gitignore`。
- 把 fallback 的 R09 文案从“移出版本库”改为“先确认是否模板占位；保留启动所需模板，轮换真实值并加入 .gitignore”，避免规则模板继续给出同样危险的默认动作。
- 在 `validate_model_output` 增加服务端兜底：R09 动作文本同时出现“删除”和 `.env` / “该文件”时，整条动作丢弃；丢弃后为空则走 fallback。
- 新增 `test_delete_env_action_is_rejected`，确保以后修改 prompt 也不会把这条建议放回来。

## 2. 模型写了不存在的规则 R99，schema 把整条动作丢掉

**场景**

为了验证“模型不许编”，用一条模型候选输出构造回归：一条动作引用真实规则 R09，另一条引用不存在的 R99。输入允许集合只有 `{"R09"}`。

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
- 这次回归还暴露了一个实现细节：不能只校验 `rule_ids` 的字段存在，还要校验它属于**本次**规则集合；否则模型可以引用另一条仓库的 R09 来“借依据”。

## 3. “remaining=0 时缓存命中不 stale”不可达成：执行方反驳，审定方修正标准

**场景**

第 7 步前置收口要求：GitHub 匿名配额 `remaining=0` 时，任一已缓存仓库仍返回 `cache.hit=true`，且 `stale=false`。原实现的 HEAD 走 `api.github.com/repos/{owner}/{repo}/commits/HEAD`，HEAD 自己也要消耗 API 配额。

**原始输出 / 原验收标准**

```text
remaining=0 时任一已缓存仓库，仍返回 cache.hit=true（不 stale）。
```

**错在哪**

这不是实现能单方面绕过的缺陷：配额已经为 0，HEAD 请求本身就会 403/429；此时只能回落到旧缓存，`cache.stale=true` 是正确行为。把“网络不可达/配额为 0”当成“缓存逻辑失败”，标准不可达成；继续按原标准验收，只会逼着执行方伪造 `hit=true` 或把 stale 藏掉，反而破坏第 6 步已经验证过的限流兜底。

**怎么改的**

- 执行方反驳：先把 HEAD 改成不消耗 API 配额的路径，再谈 `remaining=0` 的缓存验收；否则标准与 GitHub 配额事实冲突。
- 审定方修正：HEAD 改走 `https://github.com/{owner}/{repo}/commits/{branch}.atom`；缓存报告里保存 `repo.default_branch`，缓存命中只读分支名再请求 atom，不请求 API 元信息；atom 404 才回落一次元信息。
- 验收拆成两层：atom 解析 / 404 回落必须有单元测试；`remaining=0` 的真实复跑必须贴 `cache` JSON，网络不可达时记录阻塞，不伪造通过。
- 修正后真实复跑：`remaining=0` 的 octocat 请求返回 `{"hit":true,"stale":false,"cached_at":"2026-11-27T02:34:23.362621+08:00"}`；同一路径的 `/api/advice` 也能在无模型时返回 200 fallback。
