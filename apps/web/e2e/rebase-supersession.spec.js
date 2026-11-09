// Phase 3B (N7): a stale frozen edit set is rebased into a NEW draft; the predecessor is superseded.
import { test, expect } from '@playwright/test';
import crypto from 'node:crypto';

const api = process.env.IDW_API_BASE_URL || 'http://127.0.0.1:8000';
const owner = process.env.IDW_DEMO_OWNER_TOKEN;
const otherTenant = process.env.IDW_DEMO_OTHER_TENANT_TOKEN;
const SOURCE = 'claim_number,insured_name,status\nCLM-1,Asha Rao,Open\nCLM-2,Kabir Verma,Open\n';

async function call(request, method, route, data, token = owner) {
  const response = await request.fetch(`${api}${route}`, { method, data, headers: { Authorization: `Bearer ${token}` } });
  let body = null; try { body = await response.json(); } catch { /* binary */ }
  return { status: response.status(), body };
}

async function document(request) {
  const name = `rebase-${crypto.randomUUID()}.csv`;
  const uploaded = await request.fetch(`${api}/api/documents?filename=${name}`, { method: 'PUT', data: Buffer.from(SOURCE),
    headers: { Authorization: `Bearer ${owner}`, 'Content-Type': 'text/csv' } });
  const doc = await uploaded.json();
  expect((await call(request, 'POST', `/api/documents/${doc.document_id}/inspect?include_targets=true`)).status).toBe(200);
  expect((await call(request, 'PUT', `/api/documents/${doc.document_id}/category`, { category: 'insurer-authored' })).status).toBe(200);
  return doc;
}

async function frozenEdit(request, doc, versionId, targetId, value) {
  const view = await call(request, 'GET', `/api/documents/${doc.document_id}/versions/${versionId}/view?target_id=${encodeURIComponent(targetId)}`);
  const target = view.body.targets[0];
  const draft = await call(request, 'POST', `/api/documents/${doc.document_id}/edit-sets`, { reason: `edit ${targetId}` });
  expect(draft.status).toBe(200);
  const id = draft.body.edit_set_id;
  expect((await call(request, 'PUT', `/api/documents/${doc.document_id}/edit-sets/${id}/patches`,
    { patches: [{ target_id: targetId, fingerprint: target.fingerprint, new_value: { type: 'string', value } }] })).status).toBe(200);
  const preflight = await call(request, 'POST', `/api/documents/${doc.document_id}/edit-sets/${id}/preflight`);
  expect(preflight.status).toBe(200);
  return { id, frozenHash: preflight.body.frozen_hash };
}

async function editSet(request, doc, id) {
  return (await call(request, 'GET', `/api/documents/${doc.document_id}/edit-sets`)).body.items.find((item) => item.edit_set_id === id);
}

test('stale frozen edit set rebases into a new draft and keeps its frozen plan', async ({ request }) => {
  const doc = await document(request);
  const a = await frozenEdit(request, doc, doc.version_id, 'csv:2:2', 'Asha R. Rao');
  const b = await frozenEdit(request, doc, doc.version_id, 'csv:3:3', 'Closed');
  const releasedB = await call(request, 'POST', `/api/documents/${doc.document_id}/edit-sets/${b.id}/release`);
  expect(releasedB.body.outcome).toBe('VERIFIED');
  const staleRelease = await call(request, 'POST', `/api/documents/${doc.document_id}/edit-sets/${a.id}/release`);
  expect([staleRelease.status, staleRelease.body.code]).toEqual([409, 'stale_base_version']);
  const foreign = await call(request, 'POST', `/api/documents/${doc.document_id}/edit-sets/${a.id}/rebase`, undefined, otherTenant);
  expect(foreign.status).toBe(404);

  const [first, second] = await Promise.all([
    call(request, 'POST', `/api/documents/${doc.document_id}/edit-sets/${a.id}/rebase`),
    call(request, 'POST', `/api/documents/${doc.document_id}/edit-sets/${a.id}/rebase`),
  ]);
  const statuses = [first.status, second.status].sort();
  expect(statuses).toEqual([200, 409]);
  const rebased = first.status === 200 ? first.body : second.body;
  expect(rebased).toMatchObject({ previous_edit_set_id: a.id, previous_status: 'ready_for_release',
    previous_frozen_hash: a.frozenHash, base_version_id: releasedB.body.version_id, status: 'draft', patches: 1 });

  const predecessor = await editSet(request, doc, a.id);
  expect(predecessor).toMatchObject({ status: 'superseded', superseded_by: rebased.edit_set_id, base_version_id: doc.version_id });
  expect(predecessor.frozen_hash.trim()).toBe(a.frozenHash);
  const successor = await editSet(request, doc, rebased.edit_set_id);
  expect(successor).toMatchObject({ status: 'draft', supersedes: a.id, frozen_hash: null });
  const all = (await call(request, 'GET', `/api/documents/${doc.document_id}/edit-sets`)).body.items;
  expect(all.filter((item) => item.supersedes === a.id)).toHaveLength(1);

  for (const [method, path, data] of [
    ['POST', 'release'], ['POST', 'rebase'], ['POST', 'preflight'],
    ['PUT', 'patches', { patches: [{ target_id: 'csv:2:2', fingerprint: '0'.repeat(64), new_value: { type: 'string', value: 'x' } }] }],
  ]) {
    const refused = await call(request, method, `/api/documents/${doc.document_id}/edit-sets/${a.id}/${path}`, data);
    expect(refused.status, `${path} on superseded`).toBe(409);
  }

  const preflight = await call(request, 'POST', `/api/documents/${doc.document_id}/edit-sets/${rebased.edit_set_id}/preflight`);
  expect(preflight.status).toBe(200);
  const released = await call(request, 'POST', `/api/documents/${doc.document_id}/edit-sets/${rebased.edit_set_id}/release`);
  expect(released.body.outcome).toBe('VERIFIED');
  const download = await request.fetch(`${api}/api/documents/${doc.document_id}/versions/${released.body.version_id}/download`,
    { headers: { Authorization: `Bearer ${owner}` } });
  expect((await download.text())).toBe('claim_number,insured_name,status\nCLM-1,Asha R. Rao,Open\nCLM-2,Kabir Verma,Closed\n');

  const audit = (await call(request, 'GET', `/api/documents/${doc.document_id}/audit?limit=200`)).body.items;
  const superseded = audit.find((event) => event.action === 'edit_set.superseded');
  expect(superseded).toMatchObject({ subject: a.id, payload: { successor_edit_set_id: rebased.edit_set_id, frozen_hash: a.frozenHash,
    previous_status: 'ready_for_release' } });
  expect(audit.find((event) => event.action === 'edit_set.draft_created' && event.subject === rebased.edit_set_id).payload.supersedes).toBe(a.id);
  const original = await request.fetch(`${api}/api/documents/${doc.document_id}/original`, { headers: { Authorization: `Bearer ${owner}` } });
  expect(await original.text()).toBe(SOURCE);
});

test('rebase reports a conflict when the targeted value changed and leaves the edit set untouched', async ({ request }) => {
  const doc = await document(request);
  const a = await frozenEdit(request, doc, doc.version_id, 'csv:2:2', 'Asha R. Rao');
  const b = await frozenEdit(request, doc, doc.version_id, 'csv:2:2', 'Asha Rao-Iyer');
  expect((await call(request, 'POST', `/api/documents/${doc.document_id}/edit-sets/${b.id}/release`)).body.outcome).toBe('VERIFIED');
  const conflict = await call(request, 'POST', `/api/documents/${doc.document_id}/edit-sets/${a.id}/rebase`);
  expect([conflict.status, conflict.body.code]).toEqual([409, 'rebase_conflicts']);
  expect(conflict.body.details).toEqual([{ target_id: 'csv:2:2', reason: 'base_value_changed' }]);
  const predecessor = await editSet(request, doc, a.id);
  expect(predecessor).toMatchObject({ status: 'ready_for_release', superseded_by: null });
  expect(predecessor.frozen_hash.trim()).toBe(a.frozenHash);
  const all = (await call(request, 'GET', `/api/documents/${doc.document_id}/edit-sets`)).body.items;
  expect(all.filter((item) => item.supersedes === a.id)).toHaveLength(0);
});
