"""Independent oracle for migration 0008 on a database copy: re-derives every version's expected lineage class in Python (no SQL from the
migration is reused) from versions, audit events and edit sets, then compares with the migrated column. Counts only.
Usage: lineage_oracle.py expect <db> > expected.json   (BEFORE the migration)   |   lineage_oracle.py compare <db> expected.json (AFTER)"""
import collections, json, subprocess, sys
def q(db, sql):
    out = subprocess.run(["psql", "-h", "127.0.0.1", "-d", db, "-AtF", "\t", "-c", sql], capture_output=True, text=True, check=True).stdout
    return [l.split("\t") for l in out.splitlines()]
def expect(db):
    versions = {(t, i): (k, p or None, e or None) for i, t, k, p, e in q(db, "select id,tenant_id,kind,coalesce(parent_version_id::text,''),coalesce(edit_set_id::text,'') from document_versions")}
    wc, rel = set(), set()
    for t, v, action, flag in q(db, "select tenant_id, payload->>'version_id', action, coalesce(payload->>'unsigned_working_copy','') from audit_events where payload ? 'version_id' and action in ('document.text_version_saved','document.derived_version_released')"):
        if (t, v) not in versions: continue
        rel.add((t, v))
        if action == "document.text_version_saved" and flag == "true": wc.add((t, v))
    children = collections.defaultdict(list)
    for (t, i), (k, p, e) in versions.items():
        if p: children[(t, p)].append((t, i))
    def descend(roots):
        seen, stack = set(), list(roots)
        while stack:
            n = stack.pop()
            if n in seen: continue
            seen.add(n); stack.extend(children.get(n, ()))
        return seen
    wc_all = descend(wc)
    unknown_roots = {n for n, (k, p, e) in versions.items() if k == "derived" and n not in wc_all and e is None and n not in rel}
    unknown_all = descend(unknown_roots)
    out = {}
    for n, (k, p, e) in versions.items():
        out[f"{n[0]}/{n[1]}"] = "original" if k == "original" else "unsigned_working_copy" if n in wc_all else "unknown" if n in unknown_all else "standard"
    return out
if __name__ == "__main__":
    mode, db = sys.argv[1], sys.argv[2]
    if mode == "expect":
        print(json.dumps(expect(db)))
    else:
        want = json.load(open(sys.argv[3])); got = {f"{t}/{i}": c for i, t, c in q(db, "select id,tenant_id,lineage_class from document_versions")}
        diff = [k for k in want if got.get(k) != want[k]]
        print(json.dumps({"versions": len(want), "mismatches": len(diff), "expected_counts": dict(collections.Counter(want.values())), "actual_counts": dict(collections.Counter(got.values())), "extra_rows": len(set(got) - set(want))}))
        sys.exit(1 if diff or len(got) != len(want) else 0)
