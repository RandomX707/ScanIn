// Phase 3A: sidecar data corrections on read-only documents. Corrections are recorded against
// server-validated source anchors and never change the file or create a version.
import { test, expect } from '@playwright/test';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const api = process.env.IDW_API_BASE_URL || 'http://127.0.0.1:8000';
const owner = process.env.IDW_DEMO_OWNER_TOKEN;
const otherTenant = process.env.IDW_DEMO_OTHER_TENANT_TOKEN;
const fixtures = path.resolve(here, '../../../.runtime/browser-fixtures');
const sha = (bytes) => crypto.createHash('sha256').update(bytes).digest('hex');

test.beforeAll(() => {
  if (!owner || !otherTenant) throw new Error('Missing IDW_DEMO_OWNER_TOKEN / IDW_DEMO_OTHER_TENANT_TOKEN.');
});

async function apiCall(request, token, route, options = {}) {
  return request.fetch(`${api}${route}`, { ...options, headers: { Authorization: `Bearer ${token}`, ...(options.headers || {}) } });
}

async function uploadFixture(request, file, mime) {
  const bytes = fs.readFileSync(path.join(fixtures, file));
  const extension = path.extname(file);
  const filename = `${file.slice(0, -extension.length)}-${crypto.randomUUID()}${extension}`;
  const uploaded = await apiCall(request, owner, `/api/documents?filename=${encodeURIComponent(filename)}`, {
    method: 'PUT', data: bytes, headers: { 'Content-Type': mime },
  });
  expect(uploaded.ok()).toBeTruthy();
  const body = await uploaded.json();
  expect((await apiCall(request, owner, `/api/documents/${body.document_id}/inspect?include_targets=true`, { method: 'POST' })).ok()).toBeTruthy();
  return { ...body, filename, bytes };
}

async function openDocument(page, filename) {
  await page.goto('/');
  const needsIdentity = await page.evaluate((token) => sessionStorage.getItem('channel360.demo.session') !== token, owner);
  if (needsIdentity) {
    await page.evaluate((token) => { sessionStorage.setItem('channel360.demo.session', token); sessionStorage.setItem('channel360.demo.role', 'OWNER'); }, owner);
    await page.reload();
  }
  await page.getByRole('button', { name: 'Documents' }).click();
  await page.getByRole('textbox', { name: 'Search documents' }).fill(filename);
  await page.getByRole('button', { name: `Open ${filename}` }).click();
  await expect(page.locator('.source-info')).toBeVisible({ timeout: 60_000 });
}

async function selectExtractedText(page, needle) {
  const selected = await page.evaluate((text) => {
    for (const element of document.querySelectorAll('[data-anchor-source]')) {
      const node = element.firstChild;
      const index = node?.textContent?.indexOf(text) ?? -1;
      if (node && index >= 0) {
        element.closest('details')?.setAttribute('open', '');
        const range = document.createRange();
        range.setStart(node, index); range.setEnd(node, index + text.length);
        const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
        return selection.toString();
      }
    }
    return null;
  }, needle);
  expect(selected).toBe(needle);
  // Wait for the picker's own confirmation before touching any other control.
  await expect(page.locator('.anchor-pending')).toHaveText(`Selected text: “${needle}”`);
}

async function rangeRect(page, needle) {
  return page.evaluate((text) => {
    for (const element of document.querySelectorAll('[data-anchor-source]')) {
      const node = element.firstChild;
      const index = node?.textContent?.indexOf(text) ?? -1;
      if (node && index >= 0) {
        element.closest('details')?.setAttribute('open', '');
        element.scrollIntoView({ block: 'center' });
        const first = document.createRange(); first.setStart(node, index); first.setEnd(node, index + 1);
        const last = document.createRange(); last.setStart(node, index + text.length - 1); last.setEnd(node, index + text.length);
        const a = first.getBoundingClientRect(), b = last.getBoundingClientRect();
        return { x1: a.left + 1, y: a.top + a.height / 2, x2: b.right - 0.5 };
      }
    }
    return null;
  }, needle);
}

async function saveCorrection(page, label, value, reason) {
  const card = page.locator('.correction-card').filter({ hasText: label });
  await card.getByLabel('Corrected value').fill(value);
  await card.getByLabel('Correction value type').selectOption('string');
  await card.getByLabel('Correction reason').fill(reason);
  await card.getByRole('button', { name: 'Save data correction' }).click();
  await expect(page.locator('.toast')).toContainText('Data correction saved separately');
  await expect(page.locator('.correction-history-row').filter({ hasText: value })).toBeVisible();
}

async function assertSidecarOnly(request, uploaded, expectedTargetPrefix, value) {
  const versions = await (await apiCall(request, owner, `/api/documents/${uploaded.document_id}/versions`)).json();
  expect(versions.items.map((item) => item.version_id)).toEqual([uploaded.version_id]);
  const original = await apiCall(request, owner, `/api/documents/${uploaded.document_id}/original`);
  expect(sha(Buffer.from(await original.body()))).toBe(sha(uploaded.bytes));
  const exported = await (await apiCall(request, owner, `/api/documents/${uploaded.document_id}/corrections/export`)).json();
  const item = exported.find((entry) => entry.corrected_value.value === value);
  expect(item.source_sha256).toBe(sha(uploaded.bytes));
  expect(item.source_version_id).toBe(uploaded.version_id);
  expect(item.target_id.startsWith(expectedTargetPrefix)).toBeTruthy();
  expect(item.evidence[0].source_sha256).toBe(sha(uploaded.bytes));
  const foreign = await apiCall(request, otherTenant, `/api/documents/${uploaded.document_id}/corrections/export`);
  expect(foreign.status()).toBe(404);
  return item;
}

test('field-less PDF text selection becomes an audited sidecar correction', async ({ page, request }) => {
  const uploaded = await uploadFixture(request, 'synthetic-invoice-no-fields.pdf', 'application/pdf');
  await openDocument(page, uploaded.filename);
  await page.getByRole('tab', { name: 'Data corrections' }).click();
  await expect(page.getByRole('region', { name: 'Anchor text from the source' })).toBeVisible();
  await selectExtractedText(page, 'INV-SYN-50417');
  await page.getByLabel('Anchored field label').fill('Invoice number');
  await page.getByRole('button', { name: 'Add selected text as a correction field' }).click();
  await expect(page.locator('.toast')).toContainText('confirmed it against the source');
  await expect(page.locator('.correction-card').filter({ hasText: 'Invoice number' })).toContainText('INV-SYN-50417');
  await saveCorrection(page, 'Invoice number', 'INV-SYN-50418', 'Ledger shows a different invoice number');
  const item = await assertSidecarOnly(request, uploaded, 'text:pdf:page:1:', 'INV-SYN-50418');
  expect(item.observed_value.value).toBe('INV-SYN-50417');
  expect(item.evidence[0].kind).toBe('text_selection');
});

test('read-only signed PDF field is eligible for a sidecar correction but not native editing', async ({ page, request }) => {
  const uploaded = await uploadFixture(request, 'synthetic-signed.pdf', 'application/pdf');
  await openDocument(page, uploaded.filename);
  await expect(page.locator('.viewer-toolbar')).toContainText('Read only');
  const row = page.locator('.pdf-field-row').filter({ hasText: 'claimant_name' });
  await expect(row).toBeEnabled();
  await expect(row).toHaveClass(/read-only-target/);
  await row.click();
  await page.getByRole('button', { name: 'Add to data corrections →' }).click();
  await expect(page.locator('.toast')).toContainText('Field added to the correction workspace');
  await page.getByRole('tab', { name: 'Data corrections' }).click();
  await expect(page.locator('.correction-card').first()).toContainText('Read-only source value');
  await saveCorrection(page, 'claimant_name', 'Ada K. Example', 'Signed copy carries an outdated spelling');
  const item = await assertSidecarOnly(request, uploaded, 'pdf:', 'Ada K. Example');
  expect(item.evidence[0]).toMatchObject({ kind: 'native_target', natively_editable: false });
  const capability = await (await apiCall(request, owner, `/api/documents/${uploaded.document_id}/inspect`, { method: 'POST' })).json();
  expect(capability.capability.level).toBe('Read-only');
});

test('tracked-change DOCX text selection is anchored to its extracted block', async ({ page, request }) => {
  const uploaded = await uploadFixture(request, 'synthetic-tracked-changes.docx',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document');
  await openDocument(page, uploaded.filename);
  await page.getByRole('tab', { name: 'Data corrections' }).click();
  await selectExtractedText(page, 'Alexander Kumar');
  await page.getByLabel('Anchored field label').fill('Policyholder');
  await page.getByRole('button', { name: 'Add selected text as a correction field' }).click();
  await expect(page.locator('.correction-card').filter({ hasText: 'Policyholder' })).toContainText('Alexander Kumar');
  await saveCorrection(page, 'Policyholder', 'Alexander K. Kumar', 'Tracked revision not yet accepted');
  const item = await assertSidecarOnly(request, uploaded, 'text:docx:', 'Alexander K. Kumar');
  expect(item.evidence[0].extraction).toBe('docx_w_t_runs_excluding_deleted_text');
});

test('text anchors fail closed for scanned pages, edited selections and stale versions', async ({ page, request }) => {
  const scanned = await uploadFixture(request, 'synthetic-scanned.pdf', 'application/pdf');
  await openDocument(page, scanned.filename);
  await page.getByRole('tab', { name: 'Data corrections' }).click();
  await expect(page.getByText('no extractable text layer (OCR is not enabled)')).toBeVisible();
  await expect(page.locator('[data-anchor-source]')).toHaveCount(0);
  const anchor = (overrides) => ({ text_anchor: { source_version_id: scanned.version_id, kind: 'pdf_page', page: 1, start: 0, end: 4, text: 'Synt', ...overrides } });
  const scannedResponse = await apiCall(request, owner, `/api/documents/${scanned.document_id}/fields`, { method: 'POST', data: anchor({}) });
  expect([scannedResponse.status(), (await scannedResponse.json()).code]).toEqual([422, 'text_not_extractable']);
  const invoice = await uploadFixture(request, 'synthetic-invoice-no-fields.pdf', 'application/pdf');
  const forged = await apiCall(request, owner, `/api/documents/${invoice.document_id}/fields`, { method: 'POST', data: { text_anchor: {
    source_version_id: invoice.version_id, kind: 'pdf_page', page: 1, start: 0, end: 9, text: 'Fabricate' } } });
  expect([forged.status(), (await forged.json()).code]).toEqual([409, 'text_selection_mismatch']);
  const stale = await apiCall(request, owner, `/api/documents/${invoice.document_id}/fields`, { method: 'POST', data: { text_anchor: {
    source_version_id: crypto.randomUUID(), kind: 'pdf_page', page: 1, start: 0, end: 9, text: 'Synthetic' } } });
  expect([stale.status(), (await stale.json()).code]).toEqual([409, 'stale_source_version']);
  const foreign = await apiCall(request, otherTenant, `/api/documents/${invoice.document_id}/fields`, { method: 'POST', data: { text_anchor: {
    source_version_id: invoice.version_id, kind: 'pdf_page', page: 1, start: 0, end: 9, text: 'Synthetic' } } });
  expect(foreign.status()).toBe(404);
  const fields = await (await apiCall(request, owner, `/api/documents/${invoice.document_id}/fields`)).json();
  expect(fields.items).toEqual([]);
});

test('mouse selection is captured on release and survives typing a label', async ({ page, request }) => {
  const uploaded = await uploadFixture(request, 'synthetic-invoice-no-fields.pdf', 'application/pdf');
  await openDocument(page, uploaded.filename);
  await page.getByRole('tab', { name: 'Data corrections' }).click();
  const add = page.getByRole('button', { name: 'Add selected text as a correction field' });
  await expect(add).toBeDisabled();
  await expect(page.locator('.anchor-pending')).toHaveText('Select text inside one extracted block above.');
  const rect = await rangeRect(page, 'Meera Iyer');
  await page.mouse.move(rect.x1, rect.y);
  await page.mouse.down();
  await page.mouse.move(rect.x2, rect.y, { steps: 8 });
  await page.mouse.up();
  await expect(page.locator('.anchor-pending')).toHaveText('Selected text: “Meera Iyer”');
  await page.getByLabel('Anchored field label').fill('Insured');
  await page.getByLabel('Anchored field label').press('End');
  await page.getByLabel('Anchored field label').type(' name');
  await expect(page.locator('.anchor-pending')).toHaveText('Selected text: “Meera Iyer”');
  await expect(add).toBeEnabled();
  await add.click();
  await expect(page.locator('.correction-card').filter({ hasText: 'Insured name' })).toContainText('Meera Iyer');
  const fields = (await (await apiCall(request, owner, `/api/documents/${uploaded.document_id}/fields`)).json()).items;
  expect(fields.map((field) => field.observed_value.value)).toEqual(['Meera Iyer']);
  await expect(add).toBeDisabled();
});

test('keyboard selection: Ctrl/Cmd+A inside a focused block selects exactly that block', async ({ page, request }) => {
  const uploaded = await uploadFixture(request, 'synthetic-tracked-changes.docx',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document');
  await openDocument(page, uploaded.filename);
  await page.getByRole('tab', { name: 'Data corrections' }).click();
  const block = page.locator('[data-anchor-source]').filter({ hasText: 'Policyholder: Alexander Kumar' });
  await block.focus();
  await page.keyboard.press('ControlOrMeta+a');
  await expect(page.locator('.anchor-pending')).toHaveText('Selected text: “Policyholder: Alexander Kumar”');
  await page.getByLabel('Anchored field label').fill('Policyholder line');
  await page.getByRole('button', { name: 'Add selected text as a correction field' }).click();
  await expect(page.locator('.correction-card').filter({ hasText: 'Policyholder line' })).toContainText('Policyholder: Alexander Kumar');
  const fields = (await (await apiCall(request, owner, `/api/documents/${uploaded.document_id}/fields`)).json()).items;
  expect(fields[0]).toMatchObject({ observed_value: { value: 'Policyholder: Alexander Kumar' } });
  expect(fields[0].target_id).toMatch(new RegExp(`^text:docx:word/document\\.xml:p\\d+:0-${'Policyholder: Alexander Kumar'.length}$`));
});

test('selections spanning blocks are rejected and switching documents discards a pending selection', async ({ page, request }) => {
  const docx = await uploadFixture(request, 'synthetic-tracked-changes.docx',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document');
  const pdf = await uploadFixture(request, 'synthetic-invoice-no-fields.pdf', 'application/pdf');
  await openDocument(page, docx.filename);
  await page.getByRole('tab', { name: 'Data corrections' }).click();
  await selectExtractedText(page, 'Alexander Kumar');
  const spanned = await page.evaluate(() => {
    const blocks = [...document.querySelectorAll('[data-anchor-source]')];
    const range = document.createRange();
    range.setStart(blocks[0].firstChild, 2); range.setEnd(blocks[1].firstChild, 4);
    const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
    return blocks.length;
  });
  expect(spanned).toBeGreaterThan(1);
  await page.locator('.anchor-picker').dispatchEvent('mouseup');
  await expect(page.locator('.anchor-pending')).toHaveText('Select text inside one extracted block above.');
  await expect(page.getByRole('alert')).toContainText('Selections spanning several blocks are not accepted');
  await expect(page.getByRole('button', { name: 'Add selected text as a correction field' })).toBeDisabled();
  await selectExtractedText(page, 'Alexander Kumar');
  await expect(page.getByRole('button', { name: 'Add selected text as a correction field' })).toBeEnabled();
  await openDocument(page, pdf.filename);
  await page.getByRole('tab', { name: 'Data corrections' }).click();
  await expect(page.locator('.anchor-pending')).toHaveText('Select text inside one extracted block above.');
  await expect(page.getByRole('button', { name: 'Add selected text as a correction field' })).toBeDisabled();
  for (const doc of [docx, pdf]) {
    const fields = (await (await apiCall(request, owner, `/api/documents/${doc.document_id}/fields`)).json()).items;
    expect(fields).toEqual([]);
  }
});
