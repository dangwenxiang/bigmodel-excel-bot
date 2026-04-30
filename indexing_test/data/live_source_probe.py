from pathlib import Path
import json
from main import load_config
from playwright.sync_api import sync_playwright

config = load_config(Path('config.doubao.json').resolve())
url = 'https://www.doubao.com/chat/38423980981026562'
with sync_playwright() as p:
    context = p.chromium.launch_persistent_context(
        user_data_dir=str(config.browser.user_data_dir),
        headless=False,
        channel=config.browser.channel,
    )
    page = context.pages[0] if context.pages else context.new_page()
    page.goto(url, wait_until='domcontentloaded')
    page.wait_for_timeout(8000)
    payload = page.evaluate(r'''
() => {
  const norm = (s) => String(s || '').replace(/\s+/g, ' ').trim();
  const selector = 'a[href],button,[role="button"],[tabindex],div,span';
  const interesting = [];
  for (const el of Array.from(document.querySelectorAll(selector))) {
    const text = norm(el.innerText || el.textContent || '');
    const aria = norm(el.getAttribute('aria-label') || '');
    const title = norm(el.getAttribute('title') || '');
    const cls = norm(el.getAttribute('class') || '');
    const href = el.href || el.getAttribute('href') || '';
    const combined = `${text} ${aria} ${title} ${cls} ${href}`;
    if (/参考|来源|引用|网页|搜索|资料|source|reference|citation/i.test(combined)) {
      const rect = el.getBoundingClientRect();
      interesting.push({
        tag: el.tagName,
        role: el.getAttribute('role') || '',
        text: text.slice(0, 500),
        aria,
        title,
        href,
        class: cls.slice(0, 300),
        rect: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
        visible: rect.width > 0 && rect.height > 0,
        outer: (el.outerHTML || '').slice(0, 1200),
      });
    }
  }
  return {
    url: location.href,
    title: document.title,
    interesting: interesting.slice(0, 300),
    bodyText: norm(document.body.innerText).slice(0, 8000),
  };
}
''')
    out = Path('data/live-source-probe.json')
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    print(out.resolve())
    context.close()
