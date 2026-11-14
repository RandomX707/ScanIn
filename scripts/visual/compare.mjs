// Screenshots the prototype and the running app at 1440x900 and 390x844 and writes side-by-side PNGs.
// Usage (API and web already running): node scripts/visual/compare.mjs
import { chromium } from "/Users/yashaswiram/scalehealth/pocs2/Channel360/apps/web/node_modules/playwright/index.mjs";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const proto = `file://${root}/docs/design/ScanIn_Channel360_Original_Alignment_Fixed.html`;
const app = process.env.IDW_WEB_BASE_URL || "http://127.0.0.1:5174/";
const shots = path.join(root, ".runtime/shots");
const out = path.join(root, "artifacts/manager-ui");
fs.mkdirSync(shots, { recursive: true });
fs.mkdirSync(out, { recursive: true });

const tab = (n) => ["click", `.tab >> nth=${n}`];
const demo = [["click", "text=Try demo"], ["wait", 1800]];
const scenarios = {
  home: [],
  "workspace-original": demo,
  "workspace-chunks": [...demo, tab(1)],
  "workspace-edits": [...demo, tab(2)],
  history: [["click", ".nav button >> nth=1"]],
  folder: [...demo, ["click", ".version-open"]],
};
const sizes = [[1440, 900], [390, 844]];

const browser = await chromium.launch();
for (const [name, steps] of Object.entries(scenarios)) {
  for (const [w, h] of sizes) {
    for (const [label, url] of [["proto", proto], ["app", app]]) {
      const page = await browser.newPage({ viewport: { width: w, height: h } });
      await page.goto(url);
      await page.waitForTimeout(900);
      for (const [kind, a] of steps) {
        // "Try demo" is hidden on narrow screens in both the prototype and the app; dispatch the click there.
        if (kind === "click") { const l = page.locator(a).first(); if (await l.isVisible()) await l.click(); else await l.dispatchEvent("click"); }
        else await page.waitForTimeout(a);
      }
      await page.waitForTimeout(700);
      await page.screenshot({ path: path.join(shots, `${name}-${w}-${label}.png`), fullPage: true });
      await page.close();
    }
    execFileSync(path.join(root, ".venv/bin/python"), ["-c", `
from PIL import Image, ImageDraw
import sys
a=Image.open(sys.argv[1]); b=Image.open(sys.argv[2])
pad=24; head=36; h=max(a.height,b.height)
im=Image.new('RGB',(a.width+b.width+pad*3,h+head+pad),'#ffffff')
d=ImageDraw.Draw(im); d.text((pad,10),'PROTOTYPE',fill='#182925'); d.text((a.width+pad*2,10),'APP',fill='#182925')
im.paste(a,(pad,head)); im.paste(b,(a.width+pad*2,head)); im.save(sys.argv[3])
`, path.join(shots, `${name}-${w}-proto.png`), path.join(shots, `${name}-${w}-app.png`), path.join(out, `${name}-${w}x${h}.png`)]);
  }
}
await browser.close();
console.log("written to", out);
