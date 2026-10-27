# 部署说明（云端日报 + 社媒草稿）

这个 fork 在上游 Horizon 上做两件事：

1. 启用 GitHub Actions 定时日报（`.github/workflows/daily-summary.yml`）。
2. 日报写完之后，生成 X 和小红书草稿，供仓库外的 bot 稍后发布。

Horizon 不会登录 X 或小红书，也不会在这里保存这两个平台的发布凭证。

## 1. Actions 密钥

仓库 **Settings → Secrets and variables → Actions** 里配置下面这些名字。工作流把它们映射成环境变量；`data/config.github.json` 里只写变量名，不写密钥本身。

| Secret | 作用 |
| --- | --- |
| `DEEPSEEK_API_KEY` | 默认模型。`ai.provider` 仍是 `deepseek`，`ai.api_key_env` 是 `DEEPSEEK_API_KEY`。 |
| `HORIZON_WEBHOOK_URL` | 飞书 webhook。留空时跳过飞书通知，不阻止日报和草稿。 |
| `LWN_KEY` | LWN.net 全文源。没有订阅时只有这一条源失败，其他源继续。 |
| `OPENAI_API_KEY` | 仅当把 `ai.provider` 改成 `openai` 时需要。 |
| `ANTHROPIC_API_KEY` | 仅当把 `ai.provider` 改成 `anthropic` 时需要。 |
| `GOOGLE_API_KEY` | 仅当把 `ai.provider` 改成 `gemini` 时需要。 |

不要把真实密钥写进仓库、工作流 YAML 或 `data/config.github.json`。

每次运行会先执行 `cp data/config.github.json data/config.json`，再跑：

```bash
uv run horizon --hours 24
```

定时表达式保持上游模板：`0 23 * * *`（UTC 23:00，北京时间 07:00），并保留 `workflow_dispatch`，可以在 Actions 页面手动跑。GitHub 的 schedule 可能比表达式晚几分钟到几十分钟。

## 2. 打开 GitHub Pages

1. 打开仓库 **Settings → Pages**。
2. Source 选 **Deploy from a branch**。
3. Branch 选 `gh-pages`，目录选 `/ (root)`，保存。
4. 若还没有 `gh-pages` 分支，先用 **workflow_dispatch** 跑一次 Daily Horizon Summary。`peaceiris/actions-gh-pages` 会创建该分支。

工作流把 `docs/` 发布到 `gh-pages`，`keep_files: true` 会留下以前的日报。站点地址一般是 `https://<owner>.github.io/<repo>/`。

`docs/_config.yml` 里的 `baseurl` / `url` 仍是上游示例。如果 Pages 路径不是 `/Horizon`，把这两项改成你的仓库名和域名后再跑一次工作流。

## 3. 社媒草稿怎么产生

Horizon 把日报写到 `data/summaries/horizon-YYYY-MM-DD-<lang>.md`（本配置会写 `zh` 和 `en`）。下一步读取**最新一天**的日报，中文优先。

本地：

```bash
uv run python -m src.services.sns_drafts --data-dir data --config data/config.json
```

同一入口也注册成了命令：

```bash
uv run horizon-sns --data-dir data --config data/config.json
```

常用参数：

| 参数 | 作用 |
| --- | --- |
| `--date YYYY-MM-DD` | 指定某一天，而不是最新一天 |
| `--summary PATH` | 指定一份日报；同一目录里同一天的其他语言会一起读 |
| `--output-dir PATH` | 草稿根目录，默认 `<data-dir>/sns-drafts` |
| `--template` | 不调用模型，只用确定性模板 |

输出目录是 `data/sns-drafts/YYYY-MM-DD/`：

| 文件 | 内容 |
| --- | --- |
| `x.json` | 机器可读。发布 X 时用 `x_posts[].text`（1–3 条，每条不超过 280 字符，含可选话题）。`source_title` 和 `source_url` 是原文出处。`mode` 为 `llm` 或 `template`。 |
| `x.md` | 给人校对的同一批帖子，带标题和来源行。不要把整个文件贴到 X，只复制每条「正文」。 |
| `xiaohongshu.md` | 一篇小红书笔记。第一行 `# 标题` 是标题（20 字以内），后面是正文：短段落、少量 emoji、文末 3–8 个话题。平台若标题和正文分开，不要把 `# 标题` 再贴进正文。 |

同一天如果还有英文日报，字数放得下时，X 帖会多一行 `EN: ...`，此时 `x.json` 的 `bilingual` 为 `true`。只有英文日报时，帖子仍用中文起句，并保留英文标题。

链接特别长、塞不进 280 字或小红书正文预算时，正文会丢掉这条链接，但 `x.json` 的 `source_url` 仍保留完整地址。

生成策略：

- 默认按 `data/config.json` 的 `ai` 配置调用现有的 `create_ai_client`（DeepSeek 走 OpenAI 兼容接口），只做这一次改写。
- 没有密钥、请求失败，或返回的 JSON 不符合字数、语言、链接约束时，自动改用模板，仍然写出三个文件。
- 想先看模板效果：加上 `--template`。

GitHub Actions 在 `uv run horizon` 之后执行同一条命令，然后：

- 把 `data/sns-drafts/` 上传为名为 `sns-drafts` 的 artifact；
- 复制到 `docs/sns-drafts/`，随 Pages 发布。页面上的路径类似 `https://<owner>.github.io/<repo>/sns-drafts/YYYY-MM-DD/x.json`。

`data/sns-drafts/` 和 `docs/sns-drafts/` 已加入 `.gitignore`，不会进主分支。`scripts/daily-run.sh` 也会在本地日报之后生成草稿，并在同步 `docs/` 到 `gh-pages` 时带上它们。

真正发到 X 和小红书由这个仓库以外的 bot 完成。它读取 `x.json` 的 `text`，以及 `xiaohongshu.md` 的标题和正文即可。

## 4. 追加的信息源

`data/config.github.json` 保留原来的 GitHub、Hacker News、RSS、Reddit、Telegram 示例，只追加了中文 AI / 科技热点，没有删上游条目：

- RSS：量子位、新智元（地址与 `data/presets.json` 里的 wechat2rss 相同）、Solidot
- GitHub Releases：`deepseek-ai/DeepSeek-V3`
- Google News：简体中文查询 `人工智能 OR 大模型`，`hl=zh-CN`，`gl=CN`，`ceid=CN:zh-Hans`，最多 12 条

单条源失败不会挡住其他源。不需要的源把对应的 `enabled` 设为 `false`。默认 AI 提供商仍然是 DeepSeek。
