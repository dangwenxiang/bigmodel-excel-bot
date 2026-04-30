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
    page.wait_for_timeout(5000)
    clicked = False
    for text in ['参考 11 篇资料', '参考']:
        loc = page.get_by_text(text).last
        try:
            if loc.count() and loc.is_visible():
                loc.click(timeout=3000)
                clicked = True
                break
        except Exception:
            pass
    page.wait_for_timeout(3000)
    payload = page.evaluate(r'''
() => {
  const norm = (s) => String(s || '').replace(/\s+/g, ' ').trim();
  const links = Array.from(document.querySelectorAll('a[href]')).map(a => ({
    text: norm(a.innerText),
    href: a.href,
    aria: norm(a.getAttribute('aria-label') || ''),
    title: norm(a.getAttribute('title') || ''),
    class: norm(a.getAttribute('class') || ''),
    visible: (() => { const r = a.getBoundingClientRect(); return r.width > 0 && r.height > 0; })()
  }));
  const refs = [];
  for (const el of Array.from(document.querySelectorAll('div,span,button,[role="button"],a[href]'))) {
    const text = norm(el.innerText || el.textContent || '');
    const cls = norm(el.getAttribute('class') || '');
    const href = el.href || el.getAttribute('href') || '';
    if (/参考|来源|引用|资料|周大福|华西|黄金|老凤祥|中国黄金/.test(`${text} ${cls} ${href}`)) {
      const r = el.getBoundingClientRect();
      refs.push({tag: el.tagName, text: text.slice(0,500), href, class: cls.slice(0,250), visible: r.width > 0 && r.height > 0, outer: (el.outerHTML || '').slice(0,1000)});
    }
  }
  return {url: location.href, title: document.title, links, refs: refs.slice(0,200), body: norm(document.body.innerText).slice(0,10000)};
}
''')
    payload['clicked'] = clicked
    out = Path('data/live-source-click-probe.json')
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    print(out.resolve())
    context.close()
