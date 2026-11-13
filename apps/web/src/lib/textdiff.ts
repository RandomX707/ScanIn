export type Piece = { text: string; kind: "same" | "add" | "del" };
/** Word-level diff of two strings (LCS over tokens), for before/after display. */
export function wordDiff(a: string, b: string): { before: Piece[]; after: Piece[] } {
  const ta = a.split(/(\s+)/).filter((x) => x !== ""), tb = b.split(/(\s+)/).filter((x) => x !== "");
  const n = ta.length, m = tb.length;
  if (n * m > 250000) return { before: [{ text: a, kind: "del" }], after: [{ text: b, kind: "add" }] };
  const dp = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) dp[i][j] = ta[i] === tb[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  const before: Piece[] = [], after: Piece[] = [];
  let i = 0, j = 0;
  const push = (arr: Piece[], text: string, kind: Piece["kind"]) => { const last = arr[arr.length - 1]; if (last && last.kind === kind) last.text += text; else arr.push({ text, kind }); };
  while (i < n || j < m) {
    if (i < n && j < m && ta[i] === tb[j]) { push(before, ta[i], "same"); push(after, tb[j], "same"); i++; j++; }
    else if (j < m && (i === n || dp[i][j + 1] >= dp[i + 1][j])) { push(after, tb[j++], "add"); }
    else { push(before, ta[i++], "del"); }
  }
  return { before, after };
}
