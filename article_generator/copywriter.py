#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INDEXING_TEST_DIR = PROJECT_ROOT / "indexing_test"
DEFAULT_CONFIG_PATH = os.getenv("GEO_DOUBAO_CONFIG", "indexing_test/config.doubao.json")
DEFAULT_ARTICLE_OUTPUT_DIR = os.getenv("ARTICLE_GENERATOR_OUTPUT_DIR", "article_generator/outputs")
if str(INDEXING_TEST_DIR) not in sys.path:
    sys.path.insert(0, str(INDEXING_TEST_DIR))

from main import (  # noqa: E402
    AppConfig,
    ResponseData,
    click_if_present,
    ensure_chat_ready,
    get_last_response_data,
    get_response_count,
    handle_popup_if_present,
    load_config,
    open_chat_page,
    prepare_input,
    wait_for_response,
)
from material_parser.company_profile import (  # noqa: E402
    DEFAULT_UPLOAD_BUTTON_SELECTORS,
    resolve_project_path,
    sanitize_filename,
    upload_documents,
)


MARKDOWN_SUFFIXES = {".md", ".markdown"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


def collect_article_inputs(input_dir: Path) -> tuple[list[Path], list[Path]]:
    input_dir = input_dir.expanduser().resolve()
    if not input_dir.exists():
        raise FileNotFoundError(f"Input directory not found: {input_dir}")
    if not input_dir.is_dir():
        raise NotADirectoryError(f"Input path is not a directory: {input_dir}")

    markdown_files: list[Path] = []
    image_files: list[Path] = []
    for path in input_dir.rglob("*"):
        if not path.is_file() or path.name.startswith("~$"):
            continue
        suffix = path.suffix.lower()
        if suffix in MARKDOWN_SUFFIXES:
            markdown_files.append(path)
        elif suffix in IMAGE_SUFFIXES:
            image_files.append(path)

    markdown_files.sort(key=lambda item: str(item.relative_to(input_dir)).lower())
    image_files.sort(key=lambda item: str(item.relative_to(input_dir)).lower())
    if not markdown_files and not image_files:
        raise FileNotFoundError(
            f"No Markdown or image files found in {input_dir}. "
            f"Supported Markdown: {sorted(MARKDOWN_SUFFIXES)}; images: {sorted(IMAGE_SUFFIXES)}"
        )
    return markdown_files, image_files


def build_image_manifest(input_dir: Path, image_files: list[Path]) -> list[dict[str, str]]:
    manifest: list[dict[str, str]] = []
    for index, path in enumerate(image_files, start=1):
        slot = f"IMAGE_SLOT:{index:03d}"
        placeholder = "{{" + f"{slot}|file={path.name}|alt=请补充图片说明|caption=请补充图片标题" + "}}"
        manifest.append(
            {
                "slot": slot,
                "placeholder": placeholder,
                "filename": path.name,
                "relative_path": str(path.relative_to(input_dir.expanduser().resolve())),
                "absolute_path": str(path.resolve()),
            }
        )
    return manifest


def read_markdown_digest(input_dir: Path, markdown_files: list[Path], max_chars_per_file: int = 4000) -> str:
    blocks: list[str] = []
    root = input_dir.expanduser().resolve()
    for path in markdown_files:
        text = path.read_text(encoding="utf-8", errors="ignore").strip()
        if len(text) > max_chars_per_file:
            text = text[:max_chars_per_file] + "\n...（内容过长已截断，完整文件已上传给豆包）"
        blocks.append(f"## 文件：{path.relative_to(root)}\n\n{text}")
    return "\n\n".join(blocks)


def build_article_prompt(
    input_dir: Path,
    markdown_files: list[Path],
    image_manifest: list[dict[str, str]],
    topic: str,
    article_type: str,
    audience: str,
    tone: str,
    extra_instruction: str = "",
) -> str:
    root = input_dir.expanduser().resolve()
    markdown_list = "\n".join(f"- {path.relative_to(root)}" for path in markdown_files) or "- 无"
    image_list = "\n".join(
        f"- {item['slot']}：{item['relative_path']}，占位符：`{item['placeholder']}`"
        for item in image_manifest
    ) or "- 无"
    markdown_digest = read_markdown_digest(root, markdown_files) if markdown_files else "无 Markdown 文案资料。"
    extra = f"\n补充要求：{extra_instruction.strip()}\n" if extra_instruction.strip() else ""

    return f"""你是一名资深品牌营销文案策划。当前对话已上传 Markdown 文件和图片，请阅读所有上传资料，并根据图片内容判断适合插入的位置，生成一篇可直接发布或二次编辑的宣传文章。

文章主题：{topic}
文章类型：{article_type}
目标读者：{audience}
语气风格：{tone}

已上传 Markdown 文件：
{markdown_list}

已上传图片及固定占位符：
{image_list}
{extra}
Markdown 文件内容摘要如下，完整文件也已上传：

{markdown_digest}

输出要求：
1. 只输出最终宣传文章，不要输出分析过程。
2. 输出格式必须是 Markdown，适合后续转换为 HTML、公众号富文本或发布平台正文。
3. 图片不要用 Markdown 图片语法，不要生成真实图片链接；需要插图的位置必须单独一行放置上方给定的固定占位符。
4. 每个图片占位符最多使用一次，不要改写 `IMAGE_SLOT:编号` 和 `file=文件名`，可以根据上下文补充 `alt` 和 `caption`。
5. 如果某张图片不适合文章，可以不用；但不要虚构不存在的图片占位符。
6. 文章需要有吸引力标题、开头钩子、正文层次、卖点说明、信任背书、应用场景和行动号召。
7. 不要编造资料中没有的硬性事实、数字、资质或案例；如果资料不足，用稳妥表达。

推荐结构：

# 标题

开头钩子段落

{{IMAGE_SLOT:001|file=示例.png|alt=图片说明|caption=图片标题}}

## 小标题

正文段落

"""


def send_article_generation_prompt(
    page,
    config: AppConfig,
    files: list[Path],
    prompt: str,
    upload_selectors: list[str],
) -> ResponseData:
    if config.chat.new_chat_each_prompt:
        click_if_present(page, config.chat.new_chat_selectors, config.browser.action_timeout_ms)
        page.wait_for_timeout(1000)

    handle_popup_if_present(page, config)
    _, input_locator = ensure_chat_ready(page, config)
    previous_count = get_response_count(page, config.chat.response_selectors)
    previous_text = get_last_response_data(
        page,
        config,
        config.chat.response_selectors,
        include_sources=False,
    ).text

    if files:
        upload_documents(page, files, upload_selectors, input_locator=input_locator)
    prepare_input(input_locator, prompt, config.chat.clear_input_hotkey)
    if not click_if_present(page, config.chat.send_button_selectors, config.browser.action_timeout_ms):
        input_locator.press(config.chat.send_hotkey)

    handle_popup_if_present(page, config)
    return wait_for_response(page, config, previous_count, previous_text, prompt)


def write_image_manifest(output_path: Path, manifest: list[dict[str, str]]) -> Path:
    manifest_path = output_path.with_suffix(".images.json")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest_path


def generate_promotional_article(
    input_dir: Path,
    topic: str,
    article_type: str = "品牌宣传文章",
    audience: str = "潜在客户",
    tone: str = "专业、清晰、有转化力",
    config_path: Path | None = None,
    output_path: Path | None = None,
    extra_instruction: str = "",
    interactive_login: bool = True,
    upload_selectors: list[str] | None = None,
) -> tuple[Path, Path]:
    root = input_dir.expanduser().resolve()
    markdown_files, image_files = collect_article_inputs(root)
    manifest = build_image_manifest(root, image_files)
    files = [*markdown_files, *image_files]
    config_file = resolve_project_path(config_path or DEFAULT_CONFIG_PATH)
    config = load_config(config_file)

    output = output_path or (
        PROJECT_ROOT
        / DEFAULT_ARTICLE_OUTPUT_DIR
        / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{sanitize_filename(topic)}-article.md"
    )
    output = resolve_project_path(output)
    output.parent.mkdir(parents=True, exist_ok=True)

    prompt = build_article_prompt(
        input_dir=root,
        markdown_files=markdown_files,
        image_manifest=manifest,
        topic=topic,
        article_type=article_type,
        audience=audience,
        tone=tone,
        extra_instruction=extra_instruction,
    )
    selectors = upload_selectors or DEFAULT_UPLOAD_BUTTON_SELECTORS

    with open_chat_page(config, interactive_login=interactive_login) as (_, page):
        response = send_article_generation_prompt(page, config, files, prompt, selectors)

    output.write_text(response.text.strip() + "\n", encoding="utf-8")
    manifest_path = write_image_manifest(output, manifest)
    return output, manifest_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Upload Markdown and images to Doubao and generate a promotional article.")
    parser.add_argument("--input-dir", required=True, help="Directory containing Markdown and image files.")
    parser.add_argument("--topic", required=True, help="Article topic or campaign theme.")
    parser.add_argument("--article-type", default="品牌宣传文章", help="Article type, e.g. 品牌宣传文章, 公众号文章, SEO软文.")
    parser.add_argument("--audience", default="潜在客户", help="Target audience.")
    parser.add_argument("--tone", default="专业、清晰、有转化力", help="Writing tone.")
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help="Doubao runtime config path. Defaults to GEO_DOUBAO_CONFIG or indexing_test/config.doubao.json.",
    )
    parser.add_argument("--output", help="Markdown output path. Defaults to article_generator/outputs/*.md")
    parser.add_argument("--extra", default="", help="Extra instruction appended to the generation prompt.")
    parser.add_argument(
        "--no-interactive-login",
        action="store_true",
        help="Do not pause for manual login if the configured browser profile is not logged in.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    article_path, manifest_path = generate_promotional_article(
        input_dir=resolve_project_path(args.input_dir),
        topic=args.topic,
        article_type=args.article_type,
        audience=args.audience,
        tone=args.tone,
        config_path=resolve_project_path(args.config),
        output_path=resolve_project_path(args.output) if args.output else None,
        extra_instruction=args.extra,
        interactive_login=not args.no_interactive_login,
    )
    print(f"Article written to {article_path}")
    print(f"Image manifest written to {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
