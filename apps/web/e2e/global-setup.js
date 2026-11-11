import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const api = process.env.IDW_API_BASE_URL || 'http://127.0.0.1:8000';

// Every run gets its own tenant, so test documents never appear in the demo owner's workspace.
export default async function globalSetup() {
  const issued = JSON.parse(execFileSync(path.join(root, '.venv/bin/python'), [path.join(root, 'scripts/host/issue_test_token.py')]).toString());
  fs.mkdirSync(path.join(root, '.runtime'), { recursive: true });
  fs.writeFileSync(path.join(root, '.runtime/e2e-run.json'), JSON.stringify({ tenant_id: issued.tenant_id, token: issued.token }));
  // Snapshot what the demo workspace holds before the suite, to prove afterwards that nothing was added.
  const session = await (await fetch(`${api}/api/local-demo/session`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })).json();
  const folders = await (await fetch(`${api}/api/folders`, { headers: { Authorization: `Bearer ${session.access_token}` } })).json();
  fs.writeFileSync(path.join(root, '.runtime/e2e-demo-snapshot.json'), JSON.stringify(folders.items.map((f) => f.document_id)));
}
