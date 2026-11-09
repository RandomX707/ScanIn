import { test, expect } from '@playwright/test';
import crypto from 'node:crypto';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));

const api = process.env.IDW_API_BASE_URL || 'http://127.0.0.1:8000';
const owner = required('IDW_DEMO_OWNER_TOKEN');
const otherTenant = required('IDW_DEMO_OTHER_TENANT_TOKEN');

function required(name) {
  const value = process.env[name];
  if (!value) throw new Error(`Missing ${name}; provide a server-issued test identity in the shell.`);
  return value;
}

function uniqueFixtureFilename(filename) {
  const extension = path.extname(filename);
  const stem = filename.slice(0, -extension.length);
  return `${stem}-${crypto.randomUUID()}${extension}`;
}

const fixtures = [
  {
    name: 'CSV verified release', filename: 'browser-claim.csv', mime: 'text/csv',
    bytes: Buffer.from('claim_number,policy_number,loss_amount\nCLM-CSV-1001,POL-CSV-9001,1250.00\n'),
    target: 'csv:2:1', oldValue: 'CLM-CSV-1001', newValue: 'CLM-CSV-1002',
    unrelated: 'POL-CSV-9001',
  },
  {
    name: 'JSON verified release', filename: 'browser-claim.json', mime: 'application/json',
    bytes: Buffer.from('{"claim_number":"CLM-JSON-2001","policy_number":"POL-JSON-9002","loss_amount":1250.0}\n'),
    target: 'json:/claim_number', oldValue: 'CLM-JSON-2001', newValue: 'CLM-JSON-2002',
    unrelated: 'POL-JSON-9002',
  },
  {
    name: 'XLSX verified release', filename: 'm0_original.xlsx', mime: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    bytes: fs.readFileSync(path.resolve(here, '../../../artifacts/m0/m0_original.xlsx')),
    target: 'xlsx:Claim!G2', oldValue: 'constant', newValue: 'confirmed', unrelated: 'CLM-1001',
  },
  {
    name: 'PDF AcroForm verified release', filename: 'synthetic-form.pdf', mime: 'application/pdf',
    bytes: fs.readFileSync(path.resolve(here, '../../../.runtime/browser-fixtures/synthetic-form.pdf')),
    target: 'pdf:claimant_name', oldValue: 'Ada Example', newValue: 'Ada Demo', unrelated: 'Synthetic continuation page',
  },
  {
    name: 'PDF qpdf object-stream verified release', filename: 'synthetic-form-qpdf.pdf', mime: 'application/pdf',
    bytes: fs.readFileSync(path.resolve(here, '../../../.runtime/browser-fixtures/synthetic-form-qpdf.pdf')),
    target: 'pdf:claimant_name', oldValue: 'Ada Example', newValue: 'Ada Qpdf', unrelated: 'Synthetic continuation page',
  },
  {
    name: 'PDF checkbox verified release', filename: 'synthetic-form-checkbox.pdf', mime: 'application/pdf',
    bytes: fs.readFileSync(path.resolve(here, '../../../.runtime/browser-fixtures/synthetic-form.pdf')),
    target: 'pdf:consent', oldValue: 'false', newValue: 'true', nativeValueType: 'boolean',
    unrelated: 'Synthetic continuation page',
  },
  {
    name: 'PDF radio group verified release', filename: 'synthetic-form-radio.pdf', mime: 'application/pdf',
    bytes: fs.readFileSync(path.resolve(here, '../../../.runtime/browser-fixtures/synthetic-form.pdf')),
    target: 'pdf:coverage', oldValue: 'basic', newValue: 'extended', nativeValueType: 'choice',
    unrelated: 'Synthetic continuation page',
  },
  {
    name: 'PDF dropdown verified release', filename: 'synthetic-form-dropdown.pdf', mime: 'application/pdf',
    bytes: fs.readFileSync(path.resolve(here, '../../../.runtime/browser-fixtures/synthetic-form.pdf')),
    target: 'pdf:region', oldValue: 'CA', newValue: 'NY', nativeValueType: 'choice',
    unrelated: 'Synthetic continuation page',
  },
  {
    name: 'DOCX structurally verified release', filename: 'synthetic-claim.docx',
    mime: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    bytes: fs.readFileSync(path.resolve(here, '../../../.runtime/browser-fixtures/synthetic-claim.docx')),
    target: 'docx:word/document.xml:p1:r0:t0', oldValue: 'Policyholder: Alexander Kumar',
    newValue: 'Policyholder: Alexander Kumar Venkatesh', unrelated: 'CLM-DEMO-360',
  },
];

async function apiCall(request, token, route, options = {}) {
  const response = await request.fetch(`${api}${route}`, {
    ...options,
    headers: { Authorization: `Bearer ${token}`, ...(options.headers || {}) },
  });
  return response;
}

async function connect(page) {
  // Exercise backend/UI workflows against the explicitly isolated E2E tenant.
  // The dedicated sign-in test separately exercises the configured local-demo
  // issuer; this keeps repeated synthetic records out of the showcase tenant.
  const needsTestIdentity = await page.evaluate((token) =>
    sessionStorage.getItem('channel360.demo.session') !== token, owner);
  if (needsTestIdentity) {
    await page.evaluate((token) => {
      sessionStorage.setItem('channel360.demo.session', token);
      sessionStorage.setItem('channel360.demo.role', 'OWNER');
    }, owner);
    await page.reload();
  }
  await expect(page.getByRole('navigation', { name: 'Main navigation' })).toBeVisible();
  await expect(page.locator('.sidebar-user').getByText('Workspace owner', { exact: true })).toBeVisible();
}

async function selectTarget(page, targetId, adapter, oldValue = 'Policyholder: Ada Example') {
  if (adapter === 'json') {
    await page.getByRole('option').filter({ hasText: targetId.replace('json:', '') }).click();
  } else if (adapter === 'pdf') {
    await page.locator('.pdf-field-row').filter({ hasText: targetId.replace('pdf:', '') }).click();
  } else if (adapter === 'docx') {
    await page.locator('.docx-block').filter({ hasText: oldValue }).getByRole('button', { name: /Select .*target|Select for data correction/ }).click();
  } else {
    await page.locator(`.grid-value[title^="${targetId}"]`).click();
  }
}

async function readBytes(response) {
  if (!response.ok()) {
    let detail = '';
    try { const payload = await response.json(); detail = `${payload.code ?? 'download_error'}: ${payload.message ?? 'Download unavailable'}`; }
    catch { detail = 'Download unavailable'; }
    throw new Error(`Download request returned ${response.status()}: ${detail}`);
  }
  return Buffer.from(await response.body());
}

async function expectPdfCanvasRendered(page) {
  const canvas = page.locator('.pdf-workspace canvas');
  await expect(canvas).toBeVisible();
  await expect.poll(() => canvas.evaluate((element) => {
    const context = element.getContext('2d');
    if (!context || element.width === 0 || element.height === 0) return false;
    const pixels = context.getImageData(0, 0, element.width, element.height).data;
    for (let index = 0; index < pixels.length; index += 4) {
      if (pixels[index] < 245 || pixels[index + 1] < 245 || pixels[index + 2] < 245) return true;
    }
    return false;
  })).toBeTruthy();
}

async function verifiedRoundTrip({ page, request }, fixture) {
  fixture = { ...fixture, filename: uniqueFixtureFilename(fixture.filename) };
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/');
  await connect(page);
  await page.getByRole('button', { name: 'Documents' }).click();
  await page.getByLabel('Choose a document to upload').setInputFiles({ name: fixture.filename, mimeType: fixture.mime, buffer: fixture.bytes });
  await page.getByRole('button', { name: 'Upload securely' }).click();
  await expect(page.getByRole('heading', { name: fixture.filename })).toBeVisible();
  await expect(page.getByText(new RegExp(`SHA-256`))).toBeVisible();
  const adapter = fixture.mime === 'application/json' ? 'json' : fixture.mime === 'text/csv' ? 'csv' : fixture.mime === 'application/pdf' ? 'pdf' : fixture.mime === 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' ? 'docx' : 'xlsx';
  await expect(page.getByRole('tab', { name: 'Summary' })).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByTestId('structural-summary')).toContainText('Deterministic summary');
  const summaryList = await (await apiCall(request, owner, '/api/documents')).json();
  const summaryDocument = summaryList.items.find((item) => item.filename === fixture.filename);
  const summaryResponse = await apiCall(request, owner, `/api/documents/${summaryDocument.id}/versions/${summaryDocument.current_version_id}/view`);
  const summaryView = await summaryResponse.json();
  expect(summaryView.structural_summary.source_sha256).toBe(crypto.createHash('sha256').update(fixture.bytes).digest('hex'));
  expect(summaryView.structural_summary.source_document_id).toBe(summaryView.document_id);
  expect(summaryView.structural_summary.source_version_id).toBe(summaryView.version_id);
  const crossTenantSummary = await apiCall(request, otherTenant, `/api/documents/${summaryDocument.id}/versions/${summaryDocument.current_version_id}/view`);
  expect(crossTenantSummary.status()).toBe(404);
  const summaryScreenshotDir = path.resolve(here, '../../../artifacts/ui-redesign');
  fs.mkdirSync(summaryScreenshotDir, { recursive: true });
  const notification = page.getByRole('button', { name: 'Dismiss notification' });
  if (await notification.count()) await notification.click();
  await page.screenshot({ path: path.join(summaryScreenshotDir, `summary-${adapter}.png`), fullPage: true });
  if (adapter === 'pdf') {
    await expectPdfCanvasRendered(page);
    await page.getByRole('button', { name: 'Next' }).click();
    await expect(page.getByText('Page 2 of 2')).toBeVisible();
    await page.getByRole('button', { name: 'Previous' }).click();
    await expect(page.getByText('Page 1 of 2')).toBeVisible();
  }
  await selectTarget(page, fixture.target, adapter, fixture.oldValue);
  await expect(page.getByText(fixture.target, { exact: false }).first()).toBeVisible();
  if (adapter === 'docx') {
    const evidenceDir = path.resolve(here, '../../../artifacts/ui-redesign');
    fs.mkdirSync(evidenceDir, { recursive: true });
    await page.screenshot({ path: path.join(evidenceDir, 'docx-structural-preview-selection.png'), fullPage: true });
  }

  const listResponse = await apiCall(request, owner, '/api/documents');
  const docList = await listResponse.json();
  const uploaded = docList.items.find((item) => item.filename === fixture.filename);
  expect(uploaded, 'uploaded document is persisted').toBeTruthy();
  const documentId = uploaded.id;
  const originalVersion = uploaded.current_version_id;
  const originalSha = crypto.createHash('sha256').update(fixture.bytes).digest('hex');

  await connect(page);
  await page.getByRole('tab', { name: 'Native changes' }).click();
  await page.getByLabel('Document category').selectOption('insurer-authored');
  await page.getByRole('button', { name: 'Save classification' }).click();
  await expect(page.getByText('Insurer-Authored category')).toBeVisible();
  const classificationShots = path.resolve(here, '../../../artifacts/ui-redesign');
  fs.mkdirSync(classificationShots, { recursive: true });
  await page.screenshot({ path: path.join(classificationShots, 'single-owner-classification.png'), fullPage: true });
  await connect(page);

  await selectTarget(page, fixture.target, adapter, fixture.oldValue);
  await page.getByRole('button', { name: /Add to data corrections/ }).click();
  await page.getByRole('tab', { name: 'Data corrections' }).click();
  const fieldCard = page.locator('.correction-card').filter({ hasText: fixture.oldValue });
  await fieldCard.getByLabel('Corrected value').fill(`data-${fixture.newValue}`);
  await fieldCard.getByLabel('Correction value type').selectOption('string');
  await fieldCard.getByLabel('Correction reason').fill('Browser E2E sidecar correction');
  await fieldCard.getByRole('button', { name: 'Save data correction' }).click();
  await expect(page.locator('.toast')).toContainText(/Data correction saved separately/);

  await page.getByRole('tab', { name: 'Native changes' }).click();
  const screenshots = path.resolve(here, '../../../artifacts/ui-redesign');
  fs.mkdirSync(screenshots, { recursive: true });
  await page.getByLabel('Reason for document change').fill('Browser E2E native correction');
  await page.getByRole('button', { name: 'Create correction draft' }).click();
  const editCard = page.locator('.workflow-status');
  const proposedValue = editCard.getByLabel('Selected target value');
  if (await proposedValue.evaluate((element) => element.tagName) === 'SELECT') {
    await proposedValue.selectOption(fixture.newValue);
  } else {
    await proposedValue.fill(fixture.newValue);
  }
  await editCard.getByLabel('Native value type').selectOption(fixture.nativeValueType ?? 'string');
  await page.getByRole('button', { name: 'Add selected target patch' }).click();
  await expect(page.locator('.toast')).toContainText(/Native patch prepared/);
  await page.getByRole('button', { name: 'Validate changes' }).click();
  await expect(page.locator('.toast')).toContainText(/Preflight complete: Ready for release/i);
  await page.screenshot({ path: path.join(screenshots, 'owner-validation-ready.png'), fullPage: true });
  await page.getByRole('button', { name: 'Dismiss notification' }).click();
  if (adapter === 'docx') await page.screenshot({ path: path.join(screenshots, 'docx-structural-draft.png'), fullPage: true });
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: path.join(screenshots, 'native-correction-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: path.join(screenshots, 'native-correction-mobile.png') });
  await page.setViewportSize({ width: 1440, height: 1000 });

  const staleDraftResponse = await apiCall(request, owner, `/api/documents/${documentId}/edit-sets`, {
    method: 'POST', data: { reason: 'Stale-version rejection fixture' },
  });
  expect(staleDraftResponse.ok()).toBeTruthy();
  const staleDraft = await staleDraftResponse.json();

  // The owner workflow has no human-review transition; PostgreSQL integration
  // coverage verifies the ready-for-release state is explicit and approval-free.
  const editSets = await (await apiCall(request, owner, `/api/documents/${documentId}/edit-sets`)).json();
  const ready = editSets.items.find((item) => item.status === 'ready_for_release');
  expect(ready).toBeTruthy();

  const crossTenant = await apiCall(request, otherTenant, `/api/documents/${documentId}/versions`);
  expect(crossTenant.status()).toBe(404);

  await connect(page);
  await page.getByRole('tab', { name: 'Native changes' }).click();
  await page.getByRole('button', { name: 'Verify and Release' }).click();
  await page.screenshot({ path: path.join(screenshots, 'owner-verify-release-confirmation.png'), fullPage: true });
  await page.getByRole('dialog').getByRole('button', { name: 'Verify and Release' }).click();
  await expect(page.locator('.toast')).toContainText(/verified and released|structurally verified and released/i, { timeout: 30_000 });
  const verification = page.locator('.verification-result');
  const expectedOutcome = adapter === 'docx' ? 'STRUCTURALLY_VERIFIED' : 'VERIFIED';
  await expect(verification).toContainText(expectedOutcome);
  if (adapter === 'docx') {
    await expect(verification).toContainText('Structural verification: Passed');
    await expect(verification).toContainText('Visual verification: Not performed');
    await expect(verification).toContainText('Download structurally verified DOCX');
  }
  const uiVerification = JSON.parse(await verification.locator('pre').innerText());
  if (adapter === 'docx') {
    expect(uiVerification.verification_profile).toBe('structural');
    expect(uiVerification.structural_checks).toBe('PASS');
    expect(uiVerification.visual_checks).toBe('NOT_PERFORMED');
    await page.screenshot({ path: path.resolve(here, '../../../artifacts/ui-redesign/docx-structural-verification-desktop.png'), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({ path: path.resolve(here, '../../../artifacts/ui-redesign/docx-structural-verification-mobile.png') });
    await page.setViewportSize({ width: 1440, height: 1000 });
  }
  await page.screenshot({ path: path.resolve(here, '../../../artifacts/ui-redesign/verified-release-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: path.resolve(here, '../../../artifacts/ui-redesign/verified-release-mobile.png') });
  await page.setViewportSize({ width: 1440, height: 1000 });

  const releaseResponse = await apiCall(request, owner, `/api/documents/${documentId}/edit-sets/${ready.edit_set_id}/release`, { method: 'POST' });
  expect(releaseResponse.ok()).toBeTruthy();
  const release = await releaseResponse.json();
  expect(release.idempotent).toBeTruthy();
  expect(release.outcome).toBe(expectedOutcome);
  expect(release.download_available).toBeTruthy();
  const derivedVersion = release.version_id;
  const stalePreflight = await apiCall(request, owner, `/api/documents/${documentId}/edit-sets/${staleDraft.edit_set_id}/preflight`, { method: 'POST' });
  expect(stalePreflight.status()).toBe(409);
  expect((await stalePreflight.json()).code).toBe('stale_base_version');
  const download = await apiCall(request, owner, `/api/documents/${documentId}/versions/${derivedVersion}/download`);
  const derivedBytes = await readBytes(download);
  const output = path.resolve(here, '../../../artifacts/host-integration', `${adapter}-verified-derived${adapter === 'xlsx' ? '.xlsx' : `.${adapter}`}`);
  fs.mkdirSync(path.dirname(output), { recursive: true });
  fs.writeFileSync(output, derivedBytes);

  const originalResponse = await apiCall(request, owner, `/api/documents/${documentId}/original`);
  const originalBytes = await readBytes(originalResponse);
  expect(crypto.createHash('sha256').update(originalBytes).digest('hex')).toBe(originalSha);
  const view = await (await apiCall(request, owner, `/api/documents/${documentId}/versions/${derivedVersion}/view`)).json();
  const target = view.targets.find((item) => item.id === fixture.target);
  expect(String(target.value.value)).toBe(fixture.newValue);
  if (adapter === 'csv') {
    expect(derivedBytes.toString('utf8')).toContain(fixture.unrelated);
  } else if (adapter === 'json') {
    const before = JSON.parse(fixture.bytes.toString('utf8'));
    const after = JSON.parse(derivedBytes.toString('utf8'));
    expect(after.policy_number).toBe(before.policy_number);
    expect(after.loss_amount).toBe(before.loss_amount);
  } else if (adapter === 'xlsx') {
    expect(uiVerification.checks.some((check) => check.check === 'preservation' && check.status === 'PASS')).toBeTruthy();
  } else if (adapter === 'pdf') {
    expect(derivedBytes.subarray(0, fixture.bytes.length).equals(fixture.bytes)).toBeTruthy();
    expect(uiVerification.checks.some((check) => check.check === 'visual' && check.status === 'PASS')).toBeTruthy();
  } else if (adapter === 'docx') {
    const reopened = execFileSync(path.resolve(here, '../../../.venv/bin/python'), ['-c',
      'import sys; from io import BytesIO; from docx import Document; d=Document(BytesIO(sys.stdin.buffer.read())); print("FOUND" if any(p.text == "Policyholder: Alexander Kumar Venkatesh" for p in d.paragraphs) else "MISSING")'],
      { input: derivedBytes, encoding: 'utf8' }).trim();
    expect(reopened).toBe('FOUND');
    expect(uiVerification.checks.every((check) => check.status === 'PASS' || (!check.critical && check.status === 'NOT_VERIFIED'))).toBeTruthy();
  }
  const history = await (await apiCall(request, owner, `/api/documents/${documentId}/versions`)).json();
  expect(history.items.some((item) => item.version_id === originalVersion && item.sha256 === originalSha)).toBeTruthy();
  expect(history.items.some((item) => item.version_id === derivedVersion && item.verification_outcome === expectedOutcome && item.download_available)).toBeTruthy();
  const audit = await (await apiCall(request, owner, `/api/documents/${documentId}/audit`)).json();
  expect(audit.items.length).toBeGreaterThan(0);

  // A stale patch attempt after the edit set is frozen must not mutate it.
  const mutation = await apiCall(request, owner, `/api/documents/${documentId}/edit-sets/${ready.edit_set_id}/patches`, {
    method: 'PUT', data: { patches: [] },
  });
  expect([400, 409, 422]).toContain(mutation.status());

  await page.getByRole('tab', { name: 'History' }).click();
  await expect(page.getByText('Owner authorized verified release', { exact: true })).toBeVisible();
  await page.screenshot({ path: path.join(screenshots, 'owner-audit-version-history.png'), fullPage: true });
  const versionArticle = page.locator('.version-card').filter({ hasText: 'Derived version' });
  const [browserDownload] = await Promise.all([
    page.waitForEvent('download'),
    versionArticle.getByRole('button', { name: /Download/ }).click(),
  ]);
  const browserPath = path.resolve(here, '../../../artifacts/host-integration/browser', `${adapter}-${browserDownload.suggestedFilename()}`);
  fs.mkdirSync(path.dirname(browserPath), { recursive: true });
  await browserDownload.saveAs(browserPath);
  expect(fs.readFileSync(browserPath).equals(derivedBytes)).toBeTruthy();
  if (adapter === 'docx') await page.screenshot({ path: path.resolve(here, '../../../artifacts/ui-redesign/docx-structural-download-desktop.png'), fullPage: true });
  await page.screenshot({ path: path.resolve(here, '../../../artifacts/host-integration/browser', `${adapter}-verified.png`), fullPage: true });

  return { documentId, originalSha, derivedVersion, target: fixture.target, value: fixture.newValue,
    unrelated: fixture.unrelated, auditEvents: audit.items.length, output };
}

for (const fixture of fixtures) {
  test(fixture.name, async ({ page, request }) => {
    const evidence = await verifiedRoundTrip({ page, request }, fixture);
    expect(evidence.value).toBe(fixture.newValue);
  });
}

test('single owner sign-in has no role switching or reviewer workspace', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Sign in to Channel360' })).toBeVisible();
  await expect(page.getByRole('button', { name: /processor|reviewer|administrator/i })).toHaveCount(0);
  await page.getByRole('button', { name: 'Enter workspace' }).click();
  await expect(page.getByRole('navigation', { name: 'Main navigation' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Review queue' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Switch identity' })).toHaveCount(0);
  await expect(page.locator('.sidebar-user').getByText('Workspace owner', { exact: true })).toBeVisible();
  const screenshots = path.resolve(here, '../../../artifacts/ui-redesign');
  fs.mkdirSync(screenshots, { recursive: true });
  await expect(page.getByRole('heading', { name: 'Workspace overview' })).toBeVisible();
  await page.screenshot({ path: path.join(screenshots, 'dashboard.png'), fullPage: true });

  // Keep all generated records out of the configured showcase tenant.
  await page.getByRole('button', { name: 'Sign out' }).click();
  await connect(page);
  await page.getByRole('button', { name: 'Documents' }).click();
  await expect(page.getByRole('heading', { name: 'Document library' })).toBeVisible();
  await page.screenshot({ path: path.join(screenshots, 'library.png') });
  const filename = `channel360-ui-smoke-${crypto.randomUUID()}.csv`;
  await page.getByLabel('Choose a document to upload').setInputFiles({
    name: filename,
    mimeType: 'text/csv',
    buffer: Buffer.from('claim,policy\nCLM-UI-1001,POL-UI-1001\n'),
  });
  await page.getByRole('button', { name: 'Upload securely' }).click();
  await expect(page.getByRole('heading', { name: filename })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Source document', exact: true })).toHaveCount(0);
  await expect(page.locator('.source-grid')).toBeVisible();
  await page.screenshot({ path: path.join(screenshots, 'document-workspace.png'), fullPage: true });
  const firstSummaryHash = await page.locator('.source-info code').innerText();
  await page.getByRole('button', { name: 'Documents' }).click();
  const secondFilename = `channel360-summary-switch-${crypto.randomUUID()}.json`;
  await page.getByLabel('Choose a document to upload').setInputFiles({ name: secondFilename,
    mimeType: 'application/json', buffer: Buffer.from('{"unrelated":{"enabled":true}}') });
  await page.getByRole('button', { name: 'Upload securely' }).click();
  await expect(page.getByRole('heading', { name: secondFilename })).toBeVisible();
  await expect(page.getByTestId('structural-summary')).toContainText('top-level object');
  await expect(page.locator('.source-info code')).not.toHaveText(firstSummaryHash);
  await page.screenshot({ path: path.join(screenshots, 'summary-json-switch.png'), fullPage: true });
  await page.getByRole('button', { name: 'Sign out' }).click();
  await expect(page.getByRole('heading', { name: 'Sign in to Channel360' })).toBeVisible();
  await expect(page.locator('.toast')).toHaveCount(0);
});

test('Summary defaults to No AI and explicit AI generation is cached and comparable', async ({ page, request }) => {
  let calls = 0;
  await page.route(/\/api\/documents\/[^/]+\/versions\/[^/]+\/ai-summary$/, async (route) => {
    calls += 1;
    const requestUrl = new URL(route.request().url());
    const match = requestUrl.pathname.match(/\/api\/documents\/([^/]+)\/versions\/([^/]+)\/ai-summary$/);
    const view = await (await apiCall(request, owner,
      `/api/documents/${match[1]}/versions/${match[2]}/view`)).json();
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
      source_document_id: view.document_id, source_version_id: view.version_id, source_sha256: view.source_sha256,
      generation_method: 'ai', provider: 'mock-openai-compatible', model: 'synthetic-test-model',
      explanation: 'This synthetic claim record includes a claim reference and policyholder field.',
      key_points: [{ text: 'A claim reference is present.', evidence_references: [{ reference: 'csv:header', label: 'CSV header' }] }],
      evidence_references: [{ reference: 'csv:header', label: 'CSV header' }],
      limitations: ['AI-generated explanation; check important details against the original source.'],
    }) });
  });
  await page.goto('/');
  await connect(page);
  await page.getByRole('button', { name: 'Documents' }).click();
  const filename = `channel360-ai-summary-${crypto.randomUUID()}.csv`;
  await page.getByLabel('Choose a document to upload').setInputFiles({ name: filename, mimeType: 'text/csv',
    buffer: Buffer.from('claim_number,policyholder\nSYN-AI-1,Example Person\n') });
  await page.getByRole('button', { name: 'Upload securely' }).click();
  await expect(page.getByRole('heading', { name: filename })).toBeVisible();
  await expect(page.getByRole('button', { name: 'No AI' })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByText('Deterministic summary', { exact: false })).toBeVisible();
  const notification = page.getByRole('button', { name: 'Dismiss notification' });
  if (await notification.count()) await notification.click();
  expect(calls).toBe(0);
  const summaryShots = path.resolve(here, '../../../artifacts/ui-redesign');
  fs.mkdirSync(summaryShots, { recursive: true });
  await page.screenshot({ path: path.join(summaryShots, 'summary-no-ai-mode.png'), fullPage: true });
  await page.getByRole('button', { name: 'AI', exact: true }).click();
  await expect(page.getByText(/send a bounded extract.*external LLM provider/i)).toBeVisible();
  await page.getByRole('button', { name: 'Compare summaries' }).click();
  await expect(page.getByRole('heading', { name: 'Structural facts' })).toBeVisible();
  expect(calls).toBe(0);
  await page.screenshot({ path: path.join(summaryShots, 'summary-compare-mode.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  const compareColumns = await page.locator('.summary-compare-grid').evaluate((element) =>
    getComputedStyle(element).gridTemplateColumns.split(' ').length);
  expect(compareColumns).toBe(1);
  await page.getByRole('heading', { name: 'AI-generated explanation' }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(summaryShots, 'summary-compare-mode-mobile.png') });
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.getByRole('button', { name: 'AI', exact: true }).click();
  await page.getByRole('button', { name: 'Generate AI Explanation' }).click();
  await expect(page.getByText('This synthetic claim record includes a claim reference and policyholder field.')).toBeVisible();
  await expect(page.getByText('AI-generated', { exact: true })).toBeVisible();
  await page.screenshot({ path: path.join(summaryShots, 'summary-ai-mode.png'), fullPage: true });
  expect(calls).toBe(1);
  await page.getByRole('button', { name: 'No AI' }).click();
  await page.getByRole('button', { name: 'AI', exact: true }).click();
  await expect(page.getByText('This synthetic claim record includes a claim reference and policyholder field.')).toBeVisible();
  expect(calls).toBe(1);
  await page.getByRole('button', { name: 'Compare summaries' }).click();
  await expect(page.getByRole('heading', { name: 'Structural facts' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'AI-generated explanation' })).toBeVisible();
  expect(calls).toBe(1);
  await page.screenshot({ path: path.join(summaryShots, 'summary-compare-mode.png'), fullPage: true });
  await page.getByRole('button', { name: 'Documents' }).click();
  const secondFilename = `channel360-ai-summary-other-${crypto.randomUUID()}.json`;
  await page.getByLabel('Choose a document to upload').setInputFiles({ name: secondFilename, mimeType: 'application/json',
    buffer: Buffer.from('{"synthetic":{"status":"pending"}}') });
  await page.getByRole('button', { name: 'Upload securely' }).click();
  await expect(page.getByRole('heading', { name: secondFilename })).toBeVisible();
  await page.getByRole('button', { name: 'AI', exact: true }).click();
  await expect(page.getByText('This synthetic claim record includes a claim reference and policyholder field.')).toHaveCount(0);
  await expect(page.getByText('No request has been made for this document version.')).toBeVisible();
  expect(calls).toBe(1);
});

test('No AI summary remains usable after provider credential and outage errors', async ({ page }) => {
  let providerFailure = { code: 'provider_credentials_rejected', message: 'The configured AI provider credentials were rejected.' };
  let calls = 0;
  await page.route(/\/api\/documents\/[^/]+\/versions\/[^/]+\/ai-summary$/, async (route) => {
    calls += 1;
    await route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify(providerFailure) });
  });
  await page.goto('/');
  await connect(page);
  await page.getByRole('button', { name: 'Documents' }).click();
  const filename = `channel360-ai-failure-${crypto.randomUUID()}.csv`;
  await page.getByLabel('Choose a document to upload').setInputFiles({ name: filename, mimeType: 'text/csv',
    buffer: Buffer.from('claim_number,policyholder\nSYN-FAIL-1,Example Person\n') });
  await page.getByRole('button', { name: 'Upload securely' }).click();
  await expect(page.getByRole('heading', { name: filename })).toBeVisible();
  await page.getByRole('button', { name: 'AI', exact: true }).click();
  await page.getByRole('button', { name: 'Generate AI Explanation' }).click();
  await expect(page.getByRole('alert')).toContainText('credentials were rejected');
  await page.getByRole('button', { name: 'No AI' }).click();
  await expect(page.getByTestId('structural-summary')).toContainText('claim_number');

  providerFailure = { code: 'provider_unavailable', message: 'The AI provider is temporarily unavailable.' };
  await page.getByRole('button', { name: 'AI', exact: true }).click();
  await page.getByRole('button', { name: 'Generate AI Explanation' }).click();
  await expect(page.getByRole('alert')).toContainText('temporarily unavailable');
  await page.getByRole('button', { name: 'No AI' }).click();
  await expect(page.getByTestId('structural-summary')).toContainText('claim_number');
  expect(calls).toBe(2);
});

test('unsupported encrypted PDF and tracked-change DOCX remain read only in the browser', async ({ page }) => {
  const screenshots = path.resolve(here, '../../../artifacts/ui-redesign');
  fs.mkdirSync(screenshots, { recursive: true });
  await page.goto('/');
  await connect(page);
  await page.getByRole('button', { name: 'Documents' }).click();
  const encryptedName = uniqueFixtureFilename('synthetic-encrypted.pdf');
  await page.getByLabel('Choose a document to upload').setInputFiles({ name: encryptedName, mimeType: 'application/pdf',
    buffer: fs.readFileSync(path.resolve(here, '../../../.runtime/browser-fixtures/synthetic-encrypted.pdf')) });
  await page.getByRole('button', { name: 'Upload securely' }).click();
  await expect(page.getByRole('heading', { name: encryptedName })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Safe preview is unavailable' })).toBeVisible();
  await expect(page.locator('.pdf-field-row')).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Create correction draft' })).toHaveCount(0);
  await page.screenshot({ path: path.join(screenshots, 'encrypted-pdf-read-only.png'), fullPage: true });

  for (const restriction of ['certified', 'signed']) {
    await page.getByRole('button', { name: 'Documents' }).click();
    const restrictedName = uniqueFixtureFilename(`synthetic-${restriction}.pdf`);
    await page.getByLabel('Choose a document to upload').setInputFiles({ name: restrictedName, mimeType: 'application/pdf',
      buffer: fs.readFileSync(path.resolve(here, `../../../.runtime/browser-fixtures/synthetic-${restriction}.pdf`)) });
    await page.getByRole('button', { name: 'Upload securely' }).click();
    await expect(page.getByRole('heading', { name: restrictedName })).toBeVisible();
    await expect(page.getByText('PDF · Read only', { exact: true })).toBeVisible();
    await expectPdfCanvasRendered(page);
    const restrictedFields = page.locator('.pdf-field-row');
    await expect(restrictedFields).not.toHaveCount(0);
    for (const button of await restrictedFields.all()) await expect(button).toBeDisabled();
    await page.getByRole('tab', { name: 'Native changes' }).click();
    await expect(page.getByRole('button', { name: 'Create correction draft' })).toHaveCount(0);
  }

  await page.getByRole('button', { name: 'Documents' }).click();
  const xfaName = uniqueFixtureFilename('synthetic-xfa.pdf');
  await page.getByLabel('Choose a document to upload').setInputFiles({ name: xfaName, mimeType: 'application/pdf',
    buffer: fs.readFileSync(path.resolve(here, '../../../.runtime/browser-fixtures/synthetic-xfa.pdf')) });
  await page.getByRole('button', { name: 'Upload securely' }).click();
  await expect(page.getByRole('heading', { name: xfaName })).toBeVisible();
  await expect(page.getByText('PDF · Read only', { exact: true })).toBeVisible();
  await expectPdfCanvasRendered(page);
  const xfaFields = page.locator('.pdf-field-row');
  await expect(xfaFields).not.toHaveCount(0);
  for (const button of await xfaFields.all()) await expect(button).toBeDisabled();
  await page.screenshot({ path: path.join(screenshots, 'xfa-pdf-read-only.png'), fullPage: true });

  await page.getByRole('button', { name: 'Documents' }).click();
  const scannedName = uniqueFixtureFilename('synthetic-scanned.pdf');
  await page.getByLabel('Choose a document to upload').setInputFiles({ name: scannedName, mimeType: 'application/pdf',
    buffer: fs.readFileSync(path.resolve(here, '../../../.runtime/browser-fixtures/synthetic-scanned.pdf')) });
  await page.getByRole('button', { name: 'Upload securely' }).click();
  await expect(page.getByRole('heading', { name: scannedName })).toBeVisible();
  await expectPdfCanvasRendered(page);
  await expect(page.getByText('No fillable AcroForm fields were found. The PDF remains read-only.')).toBeVisible();
  await page.screenshot({ path: path.join(screenshots, 'scanned-pdf-read-only.png'), fullPage: true });

  await page.getByRole('button', { name: 'Documents' }).click();
  const trackedName = uniqueFixtureFilename('synthetic-tracked-changes.docx');
  await page.getByLabel('Choose a document to upload').setInputFiles({ name: trackedName,
    mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    buffer: fs.readFileSync(path.resolve(here, '../../../.runtime/browser-fixtures/synthetic-tracked-changes.docx')) });
  await page.getByRole('button', { name: 'Upload securely' }).click();
  await expect(page.getByRole('heading', { name: trackedName })).toBeVisible();
  await expect(page.getByText('Extracted Word text preview')).toBeVisible();
  const blockedTargets = page.locator('.text-target');
  await expect(blockedTargets).not.toHaveCount(0);
  for (const button of await blockedTargets.all()) await expect(button).toBeDisabled();
  await page.getByRole('tab', { name: 'Native changes' }).click();
  await expect(page.getByRole('button', { name: 'Create correction draft' })).toHaveCount(0);
  await page.screenshot({ path: path.join(screenshots, 'tracked-docx-read-only.png'), fullPage: true });
});

test('human review queue and approve/reject API are retired', async ({ page, request }) => {
  await page.goto('/');
  await connect(page);
  await expect(page.getByRole('button', { name: /review queue/i })).toHaveCount(0);
  await expect(page.getByRole('button', { name: /approve|reject/i })).toHaveCount(0);
  const retired = await apiCall(request, owner,
    `/api/documents/${crypto.randomUUID()}/edit-sets/${crypto.randomUUID()}/review`,
    { method: 'POST', data: { decision: 'approved' } });
  expect(retired.status()).toBe(404);
});

test('formula-dependent XLSX release is incomplete and cannot be downloaded', async ({ page, request }) => {
  const formulaFilename = uniqueFixtureFilename('formula-dependent.xlsx');
  const source = fs.readFileSync(path.resolve(here, '../../../artifacts/m0/m0_dependent_original.xlsx'));
  const uploadedResponse = await apiCall(request, owner, `/api/documents?filename=${encodeURIComponent(formulaFilename)}`, {
    method: 'PUT', data: source,
    headers: { 'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' },
  });
  expect(uploadedResponse.ok()).toBeTruthy();
  const uploaded = await uploadedResponse.json();
  const inspect = await apiCall(request, owner, `/api/documents/${uploaded.document_id}/inspect?include_targets=true`, { method: 'POST' });
  expect(inspect.ok()).toBeTruthy();
  const view = await (await apiCall(request, owner, `/api/documents/${uploaded.document_id}/versions/${uploaded.version_id}/view`)).json();
  const target = view.targets.find((item) => item.id === 'xlsx:Claim!D2');
  expect(target.value.value).toBe(125.5);
  const category = await apiCall(request, owner, `/api/documents/${uploaded.document_id}/category`, {
    method: 'PUT', data: { category: 'insurer-authored' },
  });
  expect(category.ok()).toBeTruthy();
  const draftResponse = await apiCall(request, owner, `/api/documents/${uploaded.document_id}/edit-sets`, {
    method: 'POST', data: { reason: 'Exercise formula dependency reporting' },
  });
  expect(draftResponse.ok()).toBeTruthy();
  const draft = await draftResponse.json();
  const patch = await apiCall(request, owner, `/api/documents/${uploaded.document_id}/edit-sets/${draft.edit_set_id}/patches`, {
    method: 'PUT', data: { patches: [{ target_id: target.id, fingerprint: target.fingerprint, new_value: { type: 'decimal', value: 126.5 } }] },
  });
  expect(patch.ok()).toBeTruthy();
  const preflight = await apiCall(request, owner, `/api/documents/${uploaded.document_id}/edit-sets/${draft.edit_set_id}/preflight`, { method: 'POST' });
  expect(preflight.ok()).toBeTruthy();
  const frozen = await preflight.json();
  expect(frozen.dependencies_unknown).toBeTruthy();
  expect(frozen.dependents).toContain('Totals!A1');
  expect(frozen.dependents).toContain('Claim!H2');
  await page.goto('/');
  await connect(page);
  await page.getByRole('button', { name: 'Documents' }).click();
  await page.getByRole('textbox', { name: 'Search documents' }).fill(formulaFilename);
  await page.getByRole('button', { name: `Open ${formulaFilename}` }).click();
  await page.getByRole('tab', { name: 'Native changes' }).click();
  await page.getByRole('button', { name: 'Verify and Release' }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Verify and Release' }).click();
  await expect(page.locator('.toast')).toContainText(/Verification incomplete/i, { timeout: 30_000 });
  await expect(page.locator('.verification-result')).toContainText('INCOMPLETE');
  await expect(page.locator('.workflow-status')).toContainText('Verification incomplete');
  await page.screenshot({ path: path.resolve(here, '../../../artifacts/ui-redesign/incomplete-verification-desktop.png'), fullPage: true });
  const releaseResponse = await apiCall(request, owner, `/api/documents/${uploaded.document_id}/edit-sets/${draft.edit_set_id}/release`, { method: 'POST' });
  expect(releaseResponse.ok()).toBeTruthy();
  const release = await releaseResponse.json();
  expect(release.outcome).toBe('INCOMPLETE');
  expect(release.download_available).toBeFalsy();
  const download = await apiCall(request, owner, `/api/documents/${uploaded.document_id}/versions/${release.version_id}/download`);
  expect(download.status()).toBe(409);
});
