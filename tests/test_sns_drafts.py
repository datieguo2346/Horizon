"""SNS draft generation from Horizon daily summaries."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.ai.summarizer import DailySummarizer
from src.models import (
    ClassificationResult,
    ContentAnalysis,
    ContentArtifact,
    ContentBlock,
    ContentItem,
    ProcessingResult,
    SourceType,
)
from src.services.sns_drafts import (
    XHS_BODY_LIMIT,
    XHS_TITLE_LIMIT,
    X_CHAR_LIMIT,
    build_template_draft,
    compose_x_text,
    generate_sns_drafts,
    has_cjk,
    load_digest_items,
    main,
    parse_digest,
    select_summary_files,
    write_sns_drafts,
)
from src.storage.manager import StorageManager


def _run(coro):
    return asyncio.run(coro)


def _item(
    idx: int,
    *,
    score: float,
    title_zh: str,
    title_en: str,
    summary_zh: str,
    summary_en: str,
    tags: list[str] | None = None,
    url: str | None = None,
) -> ContentItem:
    return ContentItem(
        id=f"rss:item-{idx}",
        source_type=SourceType.RSS,
        title=title_en,
        url=url or f"https://example.com/items/{idx}",
        content="content",
        author="tester",
        published_at=datetime(2026, 10, 27, 8, 0, tzinfo=timezone.utc),
        metadata={"feed_name": "量子位"},
        profile="tech-news",
        processing=ProcessingResult(
            classification=ClassificationResult(
                profile="tech-news", method="source_override"
            ),
            analysis=ContentAnalysis(
                score=score,
                reason="test",
                summary=summary_en,
                tags=tags or ["AI", "LLM"],
            ),
            artifacts={
                "zh": ContentArtifact(
                    language="zh",
                    title=title_zh,
                    blocks=[
                        ContentBlock(
                            id="summary",
                            title="摘要",
                            content=summary_zh,
                            primary=True,
                        )
                    ],
                ),
                "en": ContentArtifact(
                    language="en",
                    title=title_en,
                    blocks=[
                        ContentBlock(
                            id="summary",
                            title="Summary",
                            content=summary_en,
                            primary=True,
                        )
                    ],
                ),
            },
        ),
    )


def _write_digest(directory: Path, items: list[ContentItem]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    summarizer = DailySummarizer(
        profile_names={"tech-news": {"zh": "科技新闻", "en": "Tech News"}},
        profile_order=["tech-news"],
    )
    for language in ("zh", "en"):
        markdown = _run(
            summarizer.generate_summary(
                items,
                date="2026-10-27",
                total_fetched=12,
                language=language,
            )
        )
        (directory / f"horizon-2026-10-27-{language}.md").write_text(
            markdown, encoding="utf-8"
        )


def _sample_items() -> list[ContentItem]:
    return [
        _item(
            1,
            score=7.5,
            title_zh="开源推理引擎发布 0.9",
            title_en="Open inference engine 0.9",
            summary_zh="新版本把长上下文的显存占用降下来了。",
            summary_en="The release lowers memory use for long context.",
            tags=["LLM", "github"],
            url="https://example.com/engine",
        ),
        _item(
            2,
            score=9.2,
            title_zh="深度求索发布新模型",
            title_en="DeepSeek ships a new model",
            summary_zh="新模型把推理成本降下来了，开发者今天就能试。",
            summary_en="The new model cuts inference cost and is available today.",
            tags=["AI", "LLM"],
            url="https://example.com/deepseek",
        ),
        _item(
            3,
            score=8.4,
            title_zh="芯片厂上调明年产能",
            title_en="Chip maker raises next year's capacity",
            summary_zh="先进制程产能上调，云厂商已经在排队。",
            summary_en="Advanced-node capacity is up and cloud buyers are in line.",
            tags=["nvidia"],
            url="https://example.com/chips",
        ),
        _item(
            4,
            score=6.2,
            title_zh="第四条不该进 X 帖",
            title_en="Fourth item should not become an X post",
            summary_zh="这条分数最低。",
            summary_en="Lowest score.",
            tags=["AI"],
            url="https://example.com/fourth",
        ),
    ]


class _FakeClient:
    def __init__(self, response: str):
        self.response = response
        self.calls = 0

    async def complete(self, system: str, user: str, temperature=None, max_tokens=None):
        self.calls += 1
        assert "JSON" in system
        assert "https://example.com/deepseek" in user
        return self.response


def test_parser_reads_summarizer_output_and_ranks_by_score(tmp_path: Path):
    summaries = tmp_path / "summaries"
    _write_digest(summaries, _sample_items())
    language, items, bilingual = load_digest_items(
        {
            "zh": summaries / "horizon-2026-10-27-zh.md",
            "en": summaries / "horizon-2026-10-27-en.md",
        }
    )

    assert language == "zh"
    assert bilingual is True
    assert [item.title for item in items[:2]] == ["深度求索发布新模型", "芯片厂上调明年产能"]
    assert items[0].url == "https://example.com/deepseek"
    assert items[0].english_title == "DeepSeek ships a new model"
    assert "推理成本" in items[0].summary
    assert "AI" in items[0].tags
    assert "LLM" in items[0].tags


def test_template_drafts_fit_platform_limits():
    items = parse_digest(
        Path("tests/test_sns_drafts.py").read_text(encoding="utf-8")
    )
    # The parser must not invent items from this source file.
    assert items == []

    _summaries = DailySummarizer()
    markdown = _run(
        _summaries.generate_summary(
            _sample_items(),
            date="2026-10-27",
            total_fetched=4,
            language="zh",
        )
    )
    parsed = parse_digest(markdown)
    draft = build_template_draft(
        parsed,
        date="2026-10-27",
        language="zh",
        bilingual=True,
    )

    assert draft.mode == "template"
    assert 1 <= len(draft.posts) <= 3
    assert len(draft.posts) == 3
    assert draft.posts[0].source_title == "深度求索发布新模型"
    assert draft.posts[0].source_url == "https://example.com/deepseek"
    assert "https://example.com/fourth" not in {post.source_url for post in draft.posts}
    for post in draft.posts:
        assert post.char_count <= X_CHAR_LIMIT
        assert has_cjk(post.text)
        assert post.source_url in post.text
        assert post.hashtags
    assert draft.xhs_title
    assert len(draft.xhs_title) <= XHS_TITLE_LIMIT
    assert len(draft.xhs_body) <= XHS_BODY_LIMIT
    assert 3 <= len(draft.xhs_hashtags) <= 8
    assert all(tag.startswith("#") for tag in draft.xhs_hashtags)
    assert "1️⃣" in draft.xhs_body
    assert draft.xhs_hashtags[0] in draft.xhs_body
    assert "https://example.com/deepseek" in draft.xhs_body


def test_bilingual_post_keeps_english_title_when_it_fits():
    from src.services.sns_drafts import DigestItem

    item = DigestItem(
        title="深度求索发布新模型",
        url="https://example.com/deepseek",
        summary="推理成本下降。",
        score=9,
        tags=["AI"],
        english_title="DeepSeek ships a new model",
    )
    draft = build_template_draft(
        [item],
        date="2026-10-27",
        language="zh",
        bilingual=True,
    )

    assert draft.bilingual is True
    assert "EN: DeepSeek ships a new model" in draft.posts[0].text
    assert draft.posts[0].char_count <= X_CHAR_LIMIT


def test_long_url_stays_in_json_and_text_stays_within_limit():
    from src.services.sns_drafts import DigestItem

    long_url = "https://news.google.com/rss/articles/" + ("A" * 400)
    text = compose_x_text("中文标题", "一句摘要。", long_url, ["#人工智能"])
    assert len(text) <= X_CHAR_LIMIT
    assert long_url not in text
    assert has_cjk(text)

    draft = build_template_draft(
        [
            DigestItem(
                title="中文标题",
                url=long_url,
                summary="一句摘要。",
                score=8,
                tags=["AI"],
            )
        ],
        date="2026-10-27",
        language="zh",
        bilingual=False,
    )
    assert draft.posts[0].source_url == long_url
    assert draft.posts[0].char_count <= X_CHAR_LIMIT


def test_empty_digest_still_writes_a_note():
    draft = build_template_draft([], date="2026-10-27", language="zh", bilingual=False)

    assert len(draft.posts) == 1
    assert draft.posts[0].char_count <= X_CHAR_LIMIT
    assert has_cjk(draft.posts[0].text)
    assert draft.xhs_title == "今天比较安静"
    assert 3 <= len(draft.xhs_hashtags) <= 8


def test_english_only_digest_is_framed_in_chinese():
    from src.services.sns_drafts import DigestItem

    draft = build_template_draft(
        [
            DigestItem(
                title="DeepSeek ships a new model",
                url="https://example.com/deepseek",
                summary="Inference is cheaper.",
                score=9,
                tags=["AI"],
            )
        ],
        date="2026-10-27",
        language="en",
        bilingual=False,
    )

    assert draft.posts[0].text.startswith("热点：")
    assert "DeepSeek ships a new model" in draft.posts[0].text
    assert has_cjk(draft.posts[0].text)


def test_llm_draft_is_used_when_the_payload_is_valid():
    payload = {
        "x_posts": [
            {
                "text": "深度求索发布新模型，推理成本下降了。\nhttps://example.com/deepseek\n#人工智能",
                "hashtags": ["#人工智能"],
                "source_title": "深度求索发布新模型",
                "source_url": "https://example.com/deepseek",
            }
        ],
        "xiaohongshu": {
            "title": "AI 圈今天这 1 件事",
            "body": "1️⃣ 深度求索发布新模型\n推理成本下降了。\n🔗 https://example.com/deepseek",
            "hashtags": ["#人工智能", "#大模型", "#科技资讯"],
        },
    }
    from src.services.sns_drafts import DigestItem

    items = [
        DigestItem(
            title="深度求索发布新模型",
            url="https://example.com/deepseek",
            summary="推理成本下降了。",
            score=9,
            tags=["AI"],
        )
    ]
    client = _FakeClient(json.dumps(payload, ensure_ascii=False))
    draft = _run(
        generate_sns_drafts(
            items,
            date="2026-10-27",
            language="zh",
            bilingual=False,
            client=client,
            use_ai=True,
        )
    )

    assert client.calls == 1
    assert draft.mode == "llm"
    assert draft.posts[0].source_url == "https://example.com/deepseek"
    assert draft.posts[0].char_count <= X_CHAR_LIMIT
    assert len(draft.xhs_hashtags) >= 3


def test_invalid_llm_payload_falls_back_to_template():
    from src.services.sns_drafts import DigestItem

    items = [
        DigestItem(
            title="深度求索发布新模型",
            url="https://example.com/deepseek",
            summary="推理成本下降了。",
            score=9,
            tags=["AI"],
        )
    ]
    client = _FakeClient('{"x_posts":[{"text":"Invented https://evil.example/post"}]}')
    draft = _run(
        generate_sns_drafts(
            items,
            date="2026-10-27",
            language="zh",
            bilingual=False,
            client=client,
            use_ai=True,
        )
    )

    assert draft.mode == "template"
    assert "evil.example" not in draft.posts[0].text
    assert draft.posts[0].source_url == "https://example.com/deepseek"


def test_selects_newest_summary_and_writes_files(tmp_path: Path):
    summaries = tmp_path / "summaries"
    summaries.mkdir()
    (summaries / "horizon-2026-10-01-zh.md").write_text(
        "# Horizon 每日速递 - 2026-10-01\n\n旧日报\n",
        encoding="utf-8",
    )
    _write_digest(summaries, _sample_items()[:1])
    # Newest file is the generated 2026-10-27 digest, not the older stub.
    (summaries / "horizon-2026-10-28-en.md").write_text(
        "### [Only English](https://example.com/en) ⭐️ 9/10\n\nEnglish summary.\n",
        encoding="utf-8",
    )

    date, files = select_summary_files(summaries)
    assert date == "2026-10-28"
    assert set(files) == {"en"}

    date, files = select_summary_files(summaries, date="2026-10-27")
    language, items, bilingual = load_digest_items(files)
    draft = build_template_draft(
        items, date=date, language=language, bilingual=bilingual
    )
    written = write_sns_drafts(tmp_path / "sns-drafts", draft)

    payload = json.loads(written["x.json"].read_text(encoding="utf-8"))
    note = written["xiaohongshu.md"].read_text(encoding="utf-8")
    readable = written["x.md"].read_text(encoding="utf-8")
    assert payload["date"] == "2026-10-27"
    assert payload["x_posts"][0]["source_url"].startswith("https://")
    assert note.startswith("# ")
    assert payload["xiaohongshu"]["title"] in note
    assert payload["x_posts"][0]["source_title"] in readable
    assert "只复制每条「正文」" in readable


def test_cli_template_entrypoint(tmp_path: Path):
    summaries = tmp_path / "summaries"
    _write_digest(summaries, _sample_items()[:2])

    main(
        [
            "--data-dir",
            str(tmp_path),
            "--template",
            "--date",
            "2026-10-27",
        ]
    )

    day = tmp_path / "sns-drafts" / "2026-10-27"
    payload = json.loads((day / "x.json").read_text(encoding="utf-8"))
    assert payload["mode"] == "template"
    assert (day / "x.md").is_file()
    assert (day / "xiaohongshu.md").is_file()
    for post in payload["x_posts"]:
        assert post["char_count"] <= X_CHAR_LIMIT
        assert post["char_count"] == len(post["text"])


def test_cli_missing_summary_exits(tmp_path: Path):
    with pytest.raises(SystemExit) as caught:
        main(["--data-dir", str(tmp_path), "--template"])
    assert caught.value.code == 1


def test_github_actions_config_keeps_deepseek_and_adds_chinese_sources(monkeypatch):
    monkeypatch.setenv("LWN_KEY", "test-key")
    config = StorageManager(
        data_dir="data", config_path="data/config.github.json"
    ).load_config()

    assert config.ai.provider.value == "deepseek"
    assert config.ai.api_key_env == "DEEPSEEK_API_KEY"
    names = [source.name for source in config.sources.rss]
    assert "Simon Willison" in names
    assert "LWN.net" in names
    assert {"量子位", "新智元", "Solidot"} <= set(names)
    assert any(
        source.owner == "deepseek-ai" and source.repo == "DeepSeek-V3"
        for source in config.sources.github
    )
    assert config.sources.google_news is not None
    assert config.sources.google_news.enabled is True
    assert config.sources.google_news.language == "zh-CN"
    assert config.sources.google_news.ceid == "CN:zh-Hans"
    raw = Path("data/config.github.json").read_text(encoding="utf-8")
    assert "sk-" not in raw
    assert "AIza" not in raw


def test_daily_workflow_is_enabled_and_maps_secrets():
    workflow = Path(".github/workflows/daily-summary.yml")
    disabled = Path(".github/workflows/daily-summary.yml.disabled")
    text = workflow.read_text(encoding="utf-8")

    assert workflow.is_file()
    assert not disabled.exists()
    assert "cron: '0 23 * * *'" in text
    assert "workflow_dispatch" in text
    assert "uv run horizon --hours 24" in text
    for secret in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "LWN_KEY",
        "DEEPSEEK_API_KEY",
        "HORIZON_WEBHOOK_URL",
    ):
        assert f"secrets.{secret}" in text
    assert "python -m src.services.sns_drafts" in text
    assert "upload-artifact@v4" in text
    assert "name: sns-drafts" in text
    assert "publish_branch: gh-pages" in text
    assert "sk-" not in text
