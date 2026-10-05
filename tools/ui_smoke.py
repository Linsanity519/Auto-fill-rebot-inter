"""Isolated file-page smoke test, with fake data and all network requests blocked."""
from pathlib import Path
import argparse
import json
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable")
    args = parser.parse_args()
    errors = []
    output = ROOT / "output" / "quality"
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=args.executable)
        page = browser.new_page(viewport={"width": 1100, "height": 780})
        page.route("http://**/*", lambda route: route.abort())
        page.route("https://**/*", lambda route: route.abort())
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto((ROOT / "assets/webui/index.html").as_uri() + "?demo")
        price = page.locator('[data-name="价格策略批量开关"]')
        price.wait_for(state="attached")
        group = page.locator('.nav-group:has([data-name="价格策略批量开关"])')
        if group.evaluate("el => el.classList.contains('collapsed')"):
            group.locator('.nav-group-head').click()
        price.click()
        assert page.locator('#pricingTaskHost').is_visible()
        page.locator('#pricingTaskHost').check()
        page.evaluate("""() => {
          const handlers = {
            task_history: async () => ({root: 'fixture/data', tasks: [
              {task_id: 'task_fixture', created_at: '2026-10-04', status: 'completed', approval: {}}
            ], collection: {message: '尚待完整收集核验'}}),
            task_detail: async () => ({task: {status: 'completed'}, plan: {context: {environment: '离线夹具', identity_label: '测试会话'}, items: [{item_id: '1', name: '示例人群', before: 'on', after: 'off'}]}, items: [{item_id: '1', status: 'verified_success'}], collection: [{status: 'accepted'}], input: '<img src=x onerror=alert(1)>', result: '示例配置完成'}),
            task_imported: async () => [],
            browser_status: async () => false,
            usage_summary: async () => ({})
          };
          window.pywebview = {api: new Proxy(handlers, {get: (o,k) => o[k] || (async () => ({}))})};
        }""")
        page.locator("#btnTaskHistory").click()
        page.get_by_role("button", name="查看计划、记录与结果").click()
        page.get_by_text("技术数据（排查或核对完整内容）", exact=True).click()
        assert page.locator("dialog pre").inner_text().find("示例配置完成") >= 0
        assert page.locator("dialog pre img").count() == 0
        page.get_by_text("技术数据（排查或核对完整内容）", exact=True).click()
        page.screenshot(path=str(output / "tasks-desktop.png"), full_page=True)
        page.set_viewport_size({"width": 760, "height": 560})
        page.screenshot(path=str(output / "tasks-compact.png"), full_page=True)
        page.get_by_role("button", name="关闭", exact=True).click()
        assert not page.locator("dialog").is_visible()
        browser.close()
    if errors:
        raise AssertionError(errors)
    print(json.dumps({"passed": True, "checks": ["task history", "detail text escaping", "close",
          "1100x780 and 760x560"], "network": "blocked", "screenshots": str(output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
