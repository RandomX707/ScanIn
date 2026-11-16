import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { appendToDocxText, openDocxEditor } from './docx-edit-helpers.js';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '../../..');
const file = path.join(root, '.private/corpus/QCR/QCR/Health Quote Format.docx');
const run = () => JSON.parse(fs.readFileSync(path.join(root, '.runtime/e2e-run.json'), 'utf8'));

test.beforeEach(async ({ page }) => {
  await page.addInitScript((token) => sessionStorage.setItem('channel360.demo.session', token), run().token);
});

// Without the private evidence this file would register ZERO tests and vanish from the report. Say so explicitly instead (SKIPPED, with the prerequisite).
if (!fs.existsSync(file)) {
  test('PREREQUISITE (private stakeholder corpus): Real DOCX (Track Changes setting on)', () => {
    test.skip(true, 'private stakeholder corpus/manifest not present (.private/ and Policy Copies/ are not part of the repository); these acceptance checks did NOT run');
  });
}

test.describe('Real DOCX (Track Changes setting on, no actual revisions)', () => {
  test.skip(!fs.existsSync(file), 'private stakeholder corpus not present');
  test('opens, edits a bounded paragraph, keeps table/images/links/setting, explains tracking', async ({ page }) => {
    await page.goto('/');
    await page.setInputFiles('#file-input', file);
    await expect(page.locator('.file-main h2')).toHaveText('Health Quote Format.docx', { timeout: 90_000 });
    await page.locator('.tab', { hasText: 'Edits' }).click();
    await expect(page.locator('.empty')).toHaveCount(0);                       // not blocked: a setting is not a revision
    await openDocxEditor(page);
    expect(await page.getByTestId('target-list').locator('button').count()).toBeGreaterThan(10);
    await expect(page.getByTestId('docx-edit-viewer').locator('canvas').first()).toBeVisible({ timeout: 60_000 });   // the real pages (tables, images), not a rebuilt HTML table
    await appendToDocxText(page, 3, ' e2e');
    await expect(page.locator('#edit-state')).toHaveText('Unsaved changes');
    await page.getByRole('button', { name: 'Save as next version' }).click();
    await expect(page.locator('.toast')).toContainText('saved inside', { timeout: 120_000 });
    await expect(page.locator('.toast')).toContainText('Track Changes is turned on');
    await expect(page.locator('.file-meta')).toContainText('Version 2 of 2');
  });
});
