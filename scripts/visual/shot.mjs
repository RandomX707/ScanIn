// Usage: node shot.mjs <url> <out.png> <width> <height> [click selector ...]
import { chromium } from "/Users/yashaswiram/scalehealth/pocs2/Channel360/apps/web/node_modules/playwright/index.mjs";
const [url, out, w, h] = process.argv.slice(2);
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: +w, height: +h } });
await page.goto(url);
await page.waitForTimeout(800);
await page.screenshot({ path: out, fullPage: true });
await browser.close();
