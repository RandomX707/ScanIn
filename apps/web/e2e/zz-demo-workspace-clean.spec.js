import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const api = process.env.IDW_API_BASE_URL || 'http://127.0.0.1:8010';

// Runs last (workers: 1, alphabetical order). Acts as the local demo owner, not the suite's own tenant.
test('the demo workspace gains no test documents', async ({ page }) => {
  const before = new Set(JSON.parse(fs.readFileSync(path.join(root, '.runtime/e2e-demo-snapshot.json'), 'utf8')));
  const session = await (await fetch(`${api}/api/local-demo/session`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })).json();
  const folders = (await (await fetch(`${api}/api/folders`, { headers: { Authorization: `Bearer ${session.access_token}` } })).json()).items;
  const added = folders.filter((f) => !before.has(f.document_id));
  expect(added.map((f) => f.name)).toEqual([]);
  expect(folders.filter((f) => f.name.startsWith('e2e-')).map((f) => f.name)).toEqual([]);

  await page.goto('/');
  // Fresh isolated databases may have no demo documents yet, so the recent row can be empty.
  await expect(page.locator('.hero-copy h1')).toBeVisible();
  const recent = await page.locator('.recent-chip span').allTextContents();
  expect(recent.filter((t) => t.startsWith('e2e-'))).toEqual([]);
  await page.getByRole('button', { name: 'History' }).first().click();
  const names = await page.locator('.history-item .folder-info > strong').allTextContents();
  expect(names.filter((t) => t.startsWith('e2e-'))).toEqual([]);
});
