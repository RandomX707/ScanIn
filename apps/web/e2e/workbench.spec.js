import { test, expect } from '@playwright/test';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));

const api = process.env.IDW_API_BASE_URL || 'http://127.0.0.1:8000';
const processor = required('IDW_DEMO_PROCESSOR_TOKEN');
const administrator = required('IDW_DEMO_ADMIN_TOKEN');
const reviewer = required('IDW_DEMO_REVIEWER_TOKEN');
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
];

async function apiCall(request, token, route, options = {}) {
  const response = await request.fetch(`${api}${route}`, {
    ...options,
    headers: { Authorization: `Bearer ${token}`, ...(options.headers || {}) },
  });
  return response;
}

async function connect(page, role, filename) {
  if (await page.getByRole('heading', { name: 'Choose your demo identity' }).count() === 0) {
    await page.getByRole('button', { name: 'Switch identity' }).click();
  }
  const roleName = { processor: 'Document processor', reviewer: 'Independent reviewer', administrator: 'Workspace administrator' }[role];
  await page.getByRole('button', { name: new RegExp(roleName) }).click();
  await page.getByRole('button', { name: 'Enter workspace' }).click();
  await expect(page.getByRole('navigation', { name: 'Main navigation' })).toBeVisible();
  if (filename) {
    await page.getByRole('button', { name: 'Documents' }).click();
    await page.getByRole('textbox', { name: 'Search documents' }).fill(filename);
    await page.getByRole('button', { name: `Open ${filename}` }).click();
    await expect(page.getByText('Source document', { exact: true })).toBeVisible();
  }
}

async function selectTarget(page, targetId, adapter) {
  if (adapter === 'json') {
    await page.getByRole('option').filter({ hasText: targetId.replace('json:', '') }).click();
  } else if (adapter === 'pdf') {
    await page.locator('.pdf-field-row').filter({ hasText: targetId.replace('pdf:', '') }).click();
  } else if (adapter === 'docx') {
    await page.locator('.docx-block').filter({ hasText: 'Policyholder: Ada Example' }).getByRole('button', { name: /Select .*target|Select for data correction/ }).click();
  } else {
    await page.locator(`.grid-value[title^="${targetId}"]`).click();
  }
}

async function readBytes(response) {
  expect(response.ok()).toBeTruthy();
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
  await connect(page, 'processor');
  await page.getByRole('button', { name: 'Documents' }).click();
  await page.getByLabel('Choose a document to upload').setInputFiles({ name: fixture.filename, mimeType: fixture.mime, buffer: fixture.bytes });
  await page.getByRole('button', { name: 'Upload securely' }).click();
  await expect(page.getByRole('heading', { name: fixture.filename })).toBeVisible();
  await expect(page.getByText(new RegExp(`SHA-256`))).toBeVisible();
  const adapter = fixture.mime === 'application/json' ? 'json' : fixture.mime === 'text/csv' ? 'csv' : fixture.mime === 'application/pdf' ? 'pdf' : fixture.mime === 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' ? 'docx' : 'xlsx';
  if (adapter === 'pdf') {
    await expectPdfCanvasRendered(page);
    await page.getByRole('button', { name: 'Next' }).click();
    await expect(page.getByText('Page 2 of 2')).toBeVisible();
    await page.getByRole('button', { name: 'Previous' }).click();
    await expect(page.getByText('Page 1 of 2')).toBeVisible();
  }
  await selectTarget(page, fixture.target, adapter);
  await expect(page.getByText(fixture.target, { exact: false }).first()).toBeVisible();

  const listResponse = await apiCall(request, processor, '/api/documents');
  const docList = await listResponse.json();
  const uploaded = docList.items.find((item) => item.filename === fixture.filename);
  expect(uploaded, 'uploaded document is persisted').toBeTruthy();
  const documentId = uploaded.id;
  const originalVersion = uploaded.current_version_id;
  const originalSha = crypto.createHash('sha256').update(fixture.bytes).digest('hex');

  // Category is administrative metadata. A distinct admin identity records it;
  // the processor still needs a separately authorized processor permission.
  await connect(page, 'administrator', fixture.filename);
  await page.getByRole('tab', { name: 'Native changes' }).click();
  await page.getByLabel('Document category').selectOption('insurer-authored');
  await page.getByRole('button', { name: 'Set category (administrator only)' }).click();
  await expect(page.getByText('Insurer-Authored category')).toBeVisible();
  await connect(page, 'processor', fixture.filename);

  await selectTarget(page, fixture.target, adapter);
  await page.getByRole('button', { name: /Add to data corrections/ }).click();
  await page.getByRole('tab', { name: 'Data corrections' }).click();
  const fieldCard = page.locator('.correction-card').filter({ hasText: fixture.oldValue });
  await fieldCard.getByLabel('Corrected value').fill(`data-${fixture.newValue}`);
  await fieldCard.getByLabel('Correction value type').selectOption('string');
  await fieldCard.getByLabel('Correction reason').fill('Browser E2E sidecar correction');
  await fieldCard.getByRole('button', { name: 'Save data correction' }).click();
  await expect(page.getByRole('status')).toContainText(/Data correction saved separately/);

  await page.getByRole('tab', { name: 'Native changes' }).click();
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
  await expect(page.getByRole('status')).toContainText(/Native patch prepared/);
  await page.getByRole('button', { name: 'Preflight and freeze' }).click();
  await expect(page.getByRole('status')).toContainText(/Preflight complete: submitted/);
  const screenshots = path.resolve(here, '../../../artifacts/ui-redesign');
  fs.mkdirSync(screenshots, { recursive: true });
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.getByRole('button', { name: 'Dismiss notification' }).click();
  await page.screenshot({ path: path.join(screenshots, 'native-correction-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: path.join(screenshots, 'native-correction-mobile.png') });
  await page.setViewportSize({ width: 1440, height: 1000 });

  const staleDraftResponse = await apiCall(request, processor, `/api/documents/${documentId}/edit-sets`, {
    method: 'POST', data: { reason: 'Stale-version rejection fixture' },
  });
  expect(staleDraftResponse.ok()).toBeTruthy();
  const staleDraft = await staleDraftResponse.json();

  // PostgreSQL integration coverage independently verifies self-review is rejected.
  const editSets = await (await apiCall(request, processor, `/api/documents/${documentId}/edit-sets`)).json();
  const submitted = editSets.items.find((item) => item.status === 'submitted');
  expect(submitted).toBeTruthy();

  const crossTenant = await apiCall(request, otherTenant, `/api/documents/${documentId}/versions`);
  expect(crossTenant.status()).toBe(404);

  await connect(page, 'reviewer');
  await page.getByRole('button', { name: 'Review queue' }).click();
  await expect(page.locator('.approval-card').filter({ hasText: fixture.filename })).toBeVisible();
  fs.mkdirSync(path.resolve(here, '../../../artifacts/ui-redesign'), { recursive: true });
  await page.screenshot({ path: path.resolve(here, '../../../artifacts/ui-redesign/approval-queue-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: path.resolve(here, '../../../artifacts/ui-redesign/approval-queue-mobile.png') });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.locator('.approval-card').filter({ hasText: fixture.filename }).getByRole('button', { name: 'Approve change' }).click();
  await page.getByRole('button', { name: 'Confirm approval' }).click();
  await expect(page.getByRole('status')).toContainText(/approved/);
  await expect(page.locator('.approval-card').filter({ hasText: fixture.filename })).toHaveCount(0);
  await page.screenshot({ path: path.resolve(here, '../../../artifacts/ui-redesign/reviewer-approved-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: path.resolve(here, '../../../artifacts/ui-redesign/reviewer-approved-mobile.png') });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await connect(page, 'processor', fixture.filename);
  await page.getByRole('tab', { name: 'Native changes' }).click();
  await page.getByRole('button', { name: 'Apply, verify and release' }).click();
  await page.getByRole('button', { name: 'Confirm verified release' }).click();
  await expect(page.getByRole('status')).toContainText(/Independent verification passed/, { timeout: 30_000 });
  const verification = page.locator('.verification-result');
  await expect(verification).toContainText('VERIFIED');
  const uiVerification = JSON.parse(await verification.locator('pre').innerText());
  await page.screenshot({ path: path.resolve(here, '../../../artifacts/ui-redesign/verified-release-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: path.resolve(here, '../../../artifacts/ui-redesign/verified-release-mobile.png') });
  await page.setViewportSize({ width: 1440, height: 1000 });

  const releaseResponse = await apiCall(request, processor, `/api/documents/${documentId}/edit-sets/${submitted.edit_set_id}/release`, { method: 'POST' });
  expect(releaseResponse.ok()).toBeTruthy();
  const release = await releaseResponse.json();
  expect(release.idempotent).toBeTruthy();
  expect(release.outcome).toBe('VERIFIED');
  const derivedVersion = release.version_id;
  const stalePreflight = await apiCall(request, processor, `/api/documents/${documentId}/edit-sets/${staleDraft.edit_set_id}/preflight`, { method: 'POST' });
  expect(stalePreflight.status()).toBe(409);
  expect((await stalePreflight.json()).code).toBe('stale_base_version');
  const download = await apiCall(request, processor, `/api/documents/${documentId}/versions/${derivedVersion}/download`);
  const derivedBytes = await readBytes(download);
  const output = path.resolve(here, '../../../artifacts/host-integration', `${adapter}-verified-derived${adapter === 'xlsx' ? '.xlsx' : `.${adapter}`}`);
  fs.mkdirSync(path.dirname(output), { recursive: true });
  fs.writeFileSync(output, derivedBytes);

  const originalResponse = await apiCall(request, processor, `/api/documents/${documentId}/original`);
  const originalBytes = await readBytes(originalResponse);
  expect(crypto.createHash('sha256').update(originalBytes).digest('hex')).toBe(originalSha);
  const view = await (await apiCall(request, processor, `/api/documents/${documentId}/versions/${derivedVersion}/view`)).json();
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
  }
  const history = await (await apiCall(request, processor, `/api/documents/${documentId}/versions`)).json();
  expect(history.items.some((item) => item.version_id === originalVersion && item.sha256 === originalSha)).toBeTruthy();
  expect(history.items.some((item) => item.version_id === derivedVersion && item.verification_outcome === 'VERIFIED')).toBeTruthy();
  const audit = await (await apiCall(request, processor, `/api/documents/${documentId}/audit`)).json();
  expect(audit.items.length).toBeGreaterThan(0);

  // A stale patch attempt after the edit set is frozen must not mutate it.
  const mutation = await apiCall(request, processor, `/api/documents/${documentId}/edit-sets/${submitted.edit_set_id}/patches`, {
    method: 'PUT', data: { patches: [] },
  });
  expect([400, 409, 422]).toContain(mutation.status());

  await page.getByRole('tab', { name: 'History' }).click();
  const versionArticle = page.locator('.version-card').filter({ hasText: 'Derived version' });
  const [browserDownload] = await Promise.all([
    page.waitForEvent('download'),
    versionArticle.getByRole('button', { name: 'Download' }).click(),
  ]);
  const browserPath = path.resolve(here, '../../../artifacts/host-integration/browser', `${adapter}-${browserDownload.suggestedFilename()}`);
  fs.mkdirSync(path.dirname(browserPath), { recursive: true });
  await browserDownload.saveAs(browserPath);
  expect(fs.readFileSync(browserPath).equals(derivedBytes)).toBeTruthy();
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

test('local demo sign-in, navigation, upload and document inspection', async ({ page }) => {
  await page.goto('/');
  await connect(page, 'processor');
  const screenshots = path.resolve(here, '../../../artifacts/ui-redesign');
  fs.mkdirSync(screenshots, { recursive: true });
  await expect(page.getByRole('heading', { name: 'Workspace overview' })).toBeVisible();
  await page.screenshot({ path: path.join(screenshots, 'dashboard.png'), fullPage: true });

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
  await page.getByRole('button', { name: 'Switch identity' }).click();
  await expect(page.getByRole('heading', { name: 'Choose your demo identity' })).toBeVisible();
  await expect(page.getByRole('status')).toHaveCount(0);
});

test('DOCX preview remains read only until critical verification is available', async ({ page, request }) => {
  const fixture = fs.readFileSync(path.resolve(here, '../../../.runtime/browser-fixtures/synthetic-claim.docx'));
  const filename = uniqueFixtureFilename('synthetic-claim.docx');
  await page.goto('/');
  await connect(page, 'processor');
  await page.getByRole('button', { name: 'Documents' }).click();
  await page.getByLabel('Choose a document to upload').setInputFiles({ name: filename,
    mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', buffer: fixture });
  await page.getByRole('button', { name: 'Upload securely' }).click();
  await expect(page.getByRole('heading', { name: filename })).toBeVisible();
  await expect(page.getByText('Read-only extracted text')).toBeVisible();
  await expect(page.getByText('DOCX · Read only', { exact: true })).toBeVisible();
  await expect(page.getByText('Policyholder: Ada Example')).toBeVisible();
  const docxBlock = page.locator('.docx-block').filter({ hasText: 'Policyholder: Ada Example' });
  await docxBlock.getByRole('button', { name: 'Select for data correction' }).click();
  await expect(docxBlock.getByRole('button', { name: 'Select for data correction' })).toHaveClass(/selected/);
  const screenshots = path.resolve(here, '../../../artifacts/ui-redesign');
  fs.mkdirSync(screenshots, { recursive: true });
  await page.screenshot({ path: path.join(screenshots, 'docx-workspace-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: path.join(screenshots, 'docx-workspace-mobile.png') });
  await page.setViewportSize({ width: 1440, height: 1000 });

  const list = await (await apiCall(request, processor, '/api/documents')).json();
  const uploaded = list.items.find((item) => item.filename === filename);
  expect(uploaded).toBeTruthy();
  const docId = uploaded.id;
  const original = Buffer.from(await (await apiCall(request, processor, `/api/documents/${docId}/original`)).body());
  expect(original.equals(fixture)).toBeTruthy();
  const view = await (await apiCall(request, processor, `/api/documents/${docId}/versions/${uploaded.current_version_id}/view`)).json();
  expect(view.capability.level).toBe('read_only');
  const target = view.targets.find((entry) => entry.id.includes('document.xml:p1:'));
  expect(target.editable).toBeTruthy();
  const classified = await apiCall(request, administrator, `/api/documents/${docId}/category`, {
    method: 'PUT', data: { category: 'insurer-authored' },
  });
  expect(classified.ok()).toBeTruthy();
  const draftResponse = await apiCall(request, processor, `/api/documents/${docId}/edit-sets`, {
    method: 'POST', data: { reason: 'Synthetic DOCX browser correction' },
  });
  expect(draftResponse.ok()).toBeTruthy();
  const draft = await draftResponse.json();
  const patched = await apiCall(request, processor, `/api/documents/${docId}/edit-sets/${draft.edit_set_id}/patches`, {
    method: 'PUT', data: { patches: [{ target_id: target.id, fingerprint: target.fingerprint,
      new_value: { type: 'string', value: 'Policyholder: Casey Demo' } }] },
  });
  expect(patched.ok()).toBeTruthy();
  const preflight = await apiCall(request, processor, `/api/documents/${docId}/edit-sets/${draft.edit_set_id}/preflight`, { method: 'POST' });
  await page.getByRole('tab', { name: 'Native changes' }).click();
  await expect(page.getByText(/Native DOCX changes are disabled/)).toBeVisible();
  await expect(page.getByRole('button', { name: 'Create correction draft' })).toHaveCount(0);
  expect(preflight.status()).toBe(422);
  expect((await preflight.json()).code).toBe('critical_docx_verification_unavailable');
  const edits = await (await apiCall(request, processor, `/api/documents/${docId}/edit-sets`)).json();
  expect(edits.items.find((item) => item.edit_set_id === draft.edit_set_id).status).toBe('draft');
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: path.join(screenshots, 'docx-blocked-verification-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: path.join(screenshots, 'docx-blocked-verification-mobile.png') });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await connect(page, 'reviewer');
  await page.getByRole('button', { name: 'Review queue' }).click();
  await expect(page.locator('.approval-card').filter({ hasText: filename })).toHaveCount(0);
  const after = Buffer.from(await (await apiCall(request, processor, `/api/documents/${docId}/original`)).body());
  expect(after.equals(original)).toBeTruthy();
  const versions = await (await apiCall(request, processor, `/api/documents/${docId}/versions`)).json();
  expect(versions.items).toHaveLength(1);
  await page.screenshot({ path: path.join(screenshots, 'docx-review-queue-desktop.png'), fullPage: true });
});

test('unsupported encrypted PDF and tracked-change DOCX remain read only in the browser', async ({ page }) => {
  const screenshots = path.resolve(here, '../../../artifacts/ui-redesign');
  fs.mkdirSync(screenshots, { recursive: true });
  await page.goto('/');
  await connect(page, 'processor');
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
  await expect(page.getByText('Read-only extracted text')).toBeVisible();
  const blockedTargets = page.locator('.text-target');
  await expect(blockedTargets).not.toHaveCount(0);
  for (const button of await blockedTargets.all()) await expect(button).toBeDisabled();
  await page.getByRole('tab', { name: 'Native changes' }).click();
  await expect(page.getByRole('button', { name: 'Create correction draft' })).toHaveCount(0);
  await page.screenshot({ path: path.join(screenshots, 'tracked-docx-read-only.png'), fullPage: true });
});

test('independent reviewer can reject a submitted change', async ({ page, request }) => {
  const documentList = await (await apiCall(request, processor, '/api/documents')).json();
  const eligible = documentList.items.filter((item) => item.document_category === 'insurer-authored' && item.ingestion_state === 'ready');
  let submittedDocument;
  let submittedEdit;
  for (const item of eligible) {
    const currentView = await apiCall(request, processor, `/api/documents/${item.id}/versions/${item.current_version_id}/view`);
    if (!currentView.ok()) continue;
    const viewed = await currentView.json();
    const target = viewed.targets.find((candidate) => candidate.editable && candidate.value.type === 'string');
    if (!target) continue;
    const draftResponse = await apiCall(request, processor, `/api/documents/${item.id}/edit-sets`, {
      method: 'POST', data: { reason: 'Synthetic reviewer rejection workflow' },
    });
    if (!draftResponse.ok()) continue;
    const draft = await draftResponse.json();
    const patch = await apiCall(request, processor, `/api/documents/${item.id}/edit-sets/${draft.edit_set_id}/patches`, {
      method: 'PUT', data: { patches: [{ target_id: target.id, fingerprint: target.fingerprint, new_value: { type: 'string', value: 'Synthetic rejected proposal' } }] },
    });
    if (!patch.ok()) continue;
    const preflight = await apiCall(request, processor, `/api/documents/${item.id}/edit-sets/${draft.edit_set_id}/preflight`, { method: 'POST' });
    if (!preflight.ok()) continue;
    submittedDocument = item;
    submittedEdit = draft;
    break;
  }
  expect(submittedDocument, 'a supported synthetic document is available for reviewer rejection').toBeTruthy();
  expect(submittedEdit).toBeTruthy();

  await page.goto('/');
  await connect(page, 'reviewer');
  await page.getByRole('button', { name: 'Review queue' }).click();
  const card = page.locator('.approval-card').filter({ hasText: submittedDocument.filename });
  await expect(card).toBeVisible();
  const screenshots = path.resolve(here, '../../../artifacts/ui-redesign');
  fs.mkdirSync(screenshots, { recursive: true });
  await page.screenshot({ path: path.join(screenshots, 'reviewer-rejection-desktop.png'), fullPage: true });
  await card.getByRole('button', { name: 'Reject' }).click();
  await expect(page.getByRole('dialog')).toContainText('Your independent reviewer decision will be recorded');
  await page.getByRole('button', { name: 'Confirm rejection' }).click();
  await expect(page.getByRole('status')).toContainText(/rejected/);
  await expect(card).toHaveCount(0);
  await page.screenshot({ path: path.join(screenshots, 'reviewer-rejection-outcome-desktop.png'), fullPage: true });
  await expect(card).toHaveCount(0);
});

test('formula-dependent XLSX release is incomplete and cannot be downloaded', async ({ page, request }) => {
  const formulaFilename = uniqueFixtureFilename('formula-dependent.xlsx');
  const source = fs.readFileSync(path.resolve(here, '../../../artifacts/m0/m0_dependent_original.xlsx'));
  const uploadedResponse = await apiCall(request, processor, `/api/documents?filename=${encodeURIComponent(formulaFilename)}`, {
    method: 'PUT', data: source,
    headers: { 'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' },
  });
  expect(uploadedResponse.ok()).toBeTruthy();
  const uploaded = await uploadedResponse.json();
  const inspect = await apiCall(request, processor, `/api/documents/${uploaded.document_id}/inspect?include_targets=true`, { method: 'POST' });
  expect(inspect.ok()).toBeTruthy();
  const view = await (await apiCall(request, processor, `/api/documents/${uploaded.document_id}/versions/${uploaded.version_id}/view`)).json();
  const target = view.targets.find((item) => item.id === 'xlsx:Claim!D2');
  expect(target.value.value).toBe(125.5);
  const category = await apiCall(request, administrator, `/api/documents/${uploaded.document_id}/category`, {
    method: 'PUT', data: { category: 'insurer-authored' },
  });
  expect(category.ok()).toBeTruthy();
  const draftResponse = await apiCall(request, processor, `/api/documents/${uploaded.document_id}/edit-sets`, {
    method: 'POST', data: { reason: 'Exercise formula dependency reporting' },
  });
  expect(draftResponse.ok()).toBeTruthy();
  const draft = await draftResponse.json();
  const patch = await apiCall(request, processor, `/api/documents/${uploaded.document_id}/edit-sets/${draft.edit_set_id}/patches`, {
    method: 'PUT', data: { patches: [{ target_id: target.id, fingerprint: target.fingerprint, new_value: { type: 'decimal', value: 126.5 } }] },
  });
  expect(patch.ok()).toBeTruthy();
  const preflight = await apiCall(request, processor, `/api/documents/${uploaded.document_id}/edit-sets/${draft.edit_set_id}/preflight`, { method: 'POST' });
  expect(preflight.ok()).toBeTruthy();
  const frozen = await preflight.json();
  expect(frozen.dependencies_unknown).toBeTruthy();
  expect(frozen.dependents).toContain('Totals!A1');
  expect(frozen.dependents).toContain('Claim!H2');
  await page.goto('/');
  await connect(page, 'reviewer');
  await page.getByRole('button', { name: 'Review queue' }).click();
  const approvalCard = page.locator('.approval-card').filter({ hasText: formulaFilename });
  await expect(approvalCard).toBeVisible();
  await page.screenshot({ path: path.resolve(here, '../../../artifacts/ui-redesign/formula-approval-desktop.png'), fullPage: true });
  await approvalCard.getByRole('button', { name: 'Approve change' }).click();
  await page.getByRole('button', { name: 'Confirm approval' }).click();
  await expect(page.getByRole('status')).toContainText(/approved/);
  await connect(page, 'processor', formulaFilename);
  await page.getByRole('tab', { name: 'Native changes' }).click();
  await page.getByRole('button', { name: 'Apply, verify and release' }).click();
  await page.getByRole('button', { name: 'Confirm verified release' }).click();
  await expect(page.getByRole('status')).toContainText(/Verification incomplete/i, { timeout: 30_000 });
  await expect(page.locator('.verification-result')).toContainText('INCOMPLETE');
  await expect(page.locator('.workflow-status')).toContainText('Verification incomplete');
  await page.screenshot({ path: path.resolve(here, '../../../artifacts/ui-redesign/incomplete-verification-desktop.png'), fullPage: true });
  const releaseResponse = await apiCall(request, processor, `/api/documents/${uploaded.document_id}/edit-sets/${draft.edit_set_id}/release`, { method: 'POST' });
  expect(releaseResponse.ok()).toBeTruthy();
  const release = await releaseResponse.json();
  expect(release.outcome).toBe('INCOMPLETE');
  expect(release.download_available).toBeFalsy();
  const download = await apiCall(request, processor, `/api/documents/${uploaded.document_id}/versions/${release.version_id}/download`);
  expect(download.status()).toBe(409);
});
