// Usage: node shot.mjs <url> <out.png> <width> <height> [actions-json]
// actions: [["click","selector"],["wait",ms],["fill","selector","text"]]
import { chromium } from "/Users/yashaswiram/scalehealth/pocs2/Channel360/apps/web/node_modules/playwright/index.mjs";
const [url, out, w, h, actions] = process.argv.slice(2);
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: +w, height: +h } });
await page.goto(url);
await page.waitForTimeout(900);
for (const [kind, a, b] of JSON.parse(actions || "[]")) {
  if (kind === "click") await page.locator(a).first().click();
  else if (kind === "fill") await page.locator(a).first().fill(b);
  else if (kind === "wait") await page.waitForTimeout(a);
}
await page.waitForTimeout(700);
await page.screenshot({ path: out, fullPage: true });
await browser.close();
