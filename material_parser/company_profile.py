#!/usr/bin/env python3

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INDEXING_TEST_DIR = PROJECT_ROOT / "indexing_test"
DEFAULT_CONFIG_PATH = os.getenv("GEO_DOUBAO_CONFIG", "indexing_test/config.doubao.json")
DEFAULT_MATERIAL_OUTPUT_DIR = os.getenv("MATERIAL_PARSER_OUTPUT_DIR", "material_parser/outputs")
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
from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError  # noqa: E402


SUPPORTED_SUFFIXES = {
    ".pdf",
    ".doc",
    ".docx",
    ".txt",
    ".md",
    ".xlsx",
    ".xls",
    ".csv",
    ".ppt",
    ".pptx",
}

DEFAULT_UPLOAD_BUTTON_SELECTORS = [
    "input[type='file']",
    "button[aria-label*='上传']",
    "button[title*='上传']",
    "[role='button'][aria-label*='上传']",
    "[role='button'][title*='上传']",
    "button:has-text('上传文件')",
    "button:has-text('附件')",
    "button:has-text('上传')",
    "button:has-text('+')",
    "[class*='upload']",
    "[class*='Upload']",
    "[class*='attach']",
    "[class*='Attach']",
]


def sanitize_filename(value: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "-", value).strip("-")
    return normalized or "company-profile"


def resolve_project_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def collect_documents(input_dir: Path, suffixes: Iterable[str] = SUPPORTED_SUFFIXES) -> list[Path]:
    input_dir = input_dir.expanduser().resolve()
    if not input_dir.exists():
        raise FileNotFoundError(f"Input directory not found: {input_dir}")
    if not input_dir.is_dir():
        raise NotADirectoryError(f"Input path is not a directory: {input_dir}")

    allowed = {suffix.lower() for suffix in suffixes}
    files = [
        path
        for path in input_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in allowed and not path.name.startswith("~$")
    ]
    files.sort(key=lambda item: str(item.relative_to(input_dir)).lower())
    if not files:
        suffix_text = ", ".join(sorted(allowed))
        raise FileNotFoundError(f"No supported documents found in {input_dir}. Supported: {suffix_text}")
    return files


def build_company_profile_prompt(company_name: str, files: list[Path], extra_instruction: str = "") -> str:
    file_list = "\n".join(f"- {path.name}" for path in files)
    extra = f"\n补充要求：{extra_instruction.strip()}\n" if extra_instruction.strip() else ""
    return f"""你是一名严谨的公司资料分析师和品牌内容策划。当前对话已上传一批公司资料，请先完整阅读上传文件，再结合你的联网搜索能力补充核验公开信息，输出一份后续可直接用于文章生成的 Markdown 格式公司说明。

目标公司：{company_name}

已上传资料：
{file_list}
{extra}
要求：
1. 必须优先依据上传资料，联网搜索只用于补充、核验和发现公开资料中的重要信息。
2. 如果上传资料和联网信息存在冲突，请明确标注“资料冲突”并说明不同来源的说法。
3. 不要编造无法确认的信息；不确定的信息放入“待确认事项”。
4. 输出必须是 Markdown，结构清晰，便于后续直接根据该公司说明生成推广文章、SEO 文章或问答内容。
5. 尽量保留关键事实、数字、时间、业务范围、产品服务、优势、适用场景、客户群体、案例、资质荣誉、联系方式或地址等信息。
6. 文末给出“可用于文章生成的素材要点”和“参考来源”，参考来源需区分“上传资料”和“联网搜索”。

请按以下结构输出：

# {company_name} 公司说明

## 1. 公司概览
## 2. 主营业务与产品服务
## 3. 核心优势与差异化
## 4. 目标客户与应用场景
## 5. 关键案例、成果或数据
## 6. 品牌背书、资质与荣誉
## 7. 对外宣传口径建议
## 8. 可用于文章生成的素材要点
## 9. 待确认事项
## 10. 参考来源
"""


def upload_documents(page, files: list[Path], selectors: list[str], input_locator=None) -> None:
    file_paths = [str(path) for path in files]

    def set_existing_file_input() -> bool:
        file_inputs = page.locator("input[type='file']")
        for index in range(file_inputs.count()):
            try:
                file_inputs.nth(index).set_input_files(file_paths)
                page.wait_for_timeout(3000)
                return True
            except PlaywrightError:
                continue
        return False

    if set_existing_file_input():
        return

    # Doubao creates a hidden file input only after a real pointer click on the attachment button.
    try:
        if input_locator is not None:
            attachment_box = input_locator.evaluate(
            r"""(el) => {
            let current = el;
            for (let depth = 0; current && depth < 8; depth += 1, current = current.parentElement) {
                const buttons = Array.from(current.querySelectorAll('button,[role="button"]'))
                    .filter((button) => {
                        const rect = button.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0;
                    });
                if (buttons.length > 0) {
                    const rect = buttons[0].getBoundingClientRect();
                    return {
                        x: rect.left + rect.width / 2,
                        y: rect.top + rect.height / 2,
                    };
                }
            }
            return null;
        }"""
            )
        else:
            attachment_box = None
        if attachment_box:
            page.mouse.click(float(attachment_box["x"]), float(attachment_box["y"]))
            page.wait_for_timeout(1000)
            if set_existing_file_input():
                return
    except PlaywrightError:
        pass

    file_inputs = page.locator("input[type='file']")
    for index in range(file_inputs.count()):
        try:
            file_inputs.nth(index).set_input_files(file_paths)
            page.wait_for_timeout(3000)
            return
        except PlaywrightError:
            continue

    for selector in selectors:
        if selector == "input[type='file']":
            continue
        try:
            locator = page.locator(selector)
            if locator.count() == 0:
                continue
            with page.expect_file_chooser(timeout=5000) as chooser_info:
                locator.first.click(timeout=5000)
            chooser_info.value.set_files(file_paths)
            page.wait_for_timeout(3000)
            return
        except (PlaywrightError, PlaywrightTimeoutError):
            if set_existing_file_input():
                return
            continue

    raise RuntimeError(
        "Could not find a usable upload control. Update DEFAULT_UPLOAD_BUTTON_SELECTORS "
        "or upload the files manually in the opened Doubao page."
    )


def send_prompt_with_uploaded_files(
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

    upload_documents(page, files, upload_selectors, input_locator=input_locator)
    prepare_input(input_locator, prompt, config.chat.clear_input_hotkey)
    if not click_if_present(page, config.chat.send_button_selectors, config.browser.action_timeout_ms):
        input_locator.press(config.chat.send_hotkey)

    handle_popup_if_present(page, config)
    return wait_for_response(page, config, previous_count, previous_text, prompt)


def append_source_block(markdown: str, response: ResponseData) -> str:
    source_urls = [line.strip() for line in response.source_urls.splitlines() if line.strip()]
    source_titles = [line.strip() for line in response.source_titles.splitlines() if line.strip()]
    if not source_urls and not source_titles:
        return markdown.strip() + "\n"

    lines = [markdown.strip(), "", "## 自动提取的联网来源"]
    max_len = max(len(source_urls), len(source_titles))
    for index in range(max_len):
        title = source_titles[index] if index < len(source_titles) else "来源"
        url = source_urls[index] if index < len(source_urls) else ""
        lines.append(f"- [{title}]({url})" if url else f"- {title}")
    return "\n".join(lines).strip() + "\n"


def generate_company_profile(
    input_dir: Path,
    company_name: str | None = None,
    config_path: Path | None = None,
    output_path: Path | None = None,
    extra_instruction: str = "",
    interactive_login: bool = True,
    upload_selectors: list[str] | None = None,
) -> Path:
    files = collect_documents(input_dir)
    company = company_name or input_dir.expanduser().resolve().name
    config_file = resolve_project_path(config_path or DEFAULT_CONFIG_PATH)
    config = load_config(config_file)
    output = output_path or (
        PROJECT_ROOT
        / DEFAULT_MATERIAL_OUTPUT_DIR
        / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{sanitize_filename(company)}.md"
    )
    output = resolve_project_path(output)
    output.parent.mkdir(parents=True, exist_ok=True)

    prompt = build_company_profile_prompt(company, files, extra_instruction=extra_instruction)
    selectors = upload_selectors or DEFAULT_UPLOAD_BUTTON_SELECTORS

    with open_chat_page(config, interactive_login=interactive_login) as (_, page):
        response = send_prompt_with_uploaded_files(page, config, files, prompt, selectors)

    output.write_text(append_source_block(response.text, response), encoding="utf-8")
    return output


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Upload company materials to Doubao and generate a Markdown company profile.")
    parser.add_argument("--input-dir", required=True, help="Directory containing company material documents.")
    parser.add_argument("--company-name", help="Company name. Defaults to the input directory name.")
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help="Doubao runtime config path. Defaults to GEO_DOUBAO_CONFIG or indexing_test/config.doubao.json.",
    )
    parser.add_argument("--output", help="Markdown output path. Defaults to material_parser/outputs/*.md")
    parser.add_argument("--extra", default="", help="Extra instruction appended to the analysis prompt.")
    parser.add_argument(
        "--no-interactive-login",
        action="store_true",
        help="Do not pause for manual login if the configured browser profile is not logged in.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = generate_company_profile(
        input_dir=resolve_project_path(args.input_dir),
        company_name=args.company_name,
        config_path=resolve_project_path(args.config),
        output_path=resolve_project_path(args.output) if args.output else None,
        extra_instruction=args.extra,
        interactive_login=not args.no_interactive_login,
    )
    print(f"Company profile written to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
