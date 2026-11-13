/** Display-only Excel number/date formatting. It never changes a stored value; unknown codes fall back to the raw text. */
const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
const DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

function stripQuoted(code: string): { tokens: string[]; literal: string[] } {
  // Split a section into alternating tokens; quoted/escaped text becomes literals that are emitted verbatim.
  const tokens: string[] = []; const literal: string[] = [];
  let i = 0; let buf = "";
  const flush = () => { if (buf) { tokens.push(buf); literal.push(""); buf = ""; } };
  while (i < code.length) {
    const ch = code[i];
    if (ch === '"') { flush(); const end = code.indexOf('"', i + 1); const text = code.slice(i + 1, end < 0 ? undefined : end); tokens.push(""); literal.push(text); i = end < 0 ? code.length : end + 1; continue; }
    if (ch === "\\" && i + 1 < code.length) { flush(); tokens.push(""); literal.push(code[i + 1]); i += 2; continue; }
    if (ch === "_" || ch === "*") { flush(); i += 2; if (ch === "_") { tokens.push(""); literal.push(" "); } continue; }
    if (ch === "[") { flush(); const end = code.indexOf("]", i); i = end < 0 ? code.length : end + 1; continue; }
    buf += ch; i++;
  }
  flush();
  return { tokens, literal };
}

const isDateCode = (code: string) => /(^|[^a-z])(d{1,4}|m{1,5}|y{2,4}|h{1,2}|s{1,2})([^a-z]|$)/i.test(code.replace(/"[^"]*"/g, "").replace(/\\./g, "").replace(/\[[^\]]*\]/g, ""));
const pad = (n: number, w = 2) => String(n).padStart(w, "0");

function formatDate(iso: string, code: string): string | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?/.exec(iso);
  if (!m) return null;
  const [y, mo, d] = [Number(m[1]), Number(m[2]), Number(m[3])];
  const hh = Number(m[4] ?? 0), mi = Number(m[5] ?? 0), ss = Number(m[6] ?? 0);
  const dow = new Date(Date.UTC(y, mo - 1, d)).getUTCDay();
  const ampm = /AM\/PM|A\/P/i.test(code);
  const { tokens, literal } = stripQuoted(code.split(";")[0]);
  let out = "";
  let afterHour = false;
  tokens.forEach((t, idx) => {
    if (!t) { out += literal[idx]; return; }
    out += t.replace(/AM\/PM|A\/P|yyyy|yy|mmmmm|mmmm|mmm|mm|m|dddd|ddd|dd|d|hh|h|ss|s/gi, (tok) => {
      const k = tok.toLowerCase();
      switch (k) {
        case "yyyy": return String(y); case "yy": return pad(y % 100);
        case "mmmm": return MONTHS[mo - 1]; case "mmm": return MONTHS[mo - 1].slice(0, 3); case "mmmmm": return MONTHS[mo - 1][0];
        case "mm": { const r = afterHour ? pad(mi) : pad(mo); afterHour = false; return r; }
        case "m": { const r = afterHour ? String(mi) : String(mo); afterHour = false; return r; }
        case "dddd": return DAYS[dow]; case "ddd": return DAYS[dow].slice(0, 3); case "dd": return pad(d); case "d": return String(d);
        case "hh": afterHour = true; return pad(ampm ? (hh % 12) || 12 : hh); case "h": afterHour = true; return String(ampm ? (hh % 12) || 12 : hh);
        case "ss": return pad(ss); case "s": return String(ss);
        default: return hh >= 12 ? "PM" : "AM";
      }
    });
  });
  return out;
}

export function formatCell(raw: string, type: string | null | undefined, code: string | undefined): string {
  if (!code || raw === "" || raw == null) return raw;
  try {
    if (type === "date" || type === "datetime") return (isDateCode(code) ? formatDate(raw, code) : null) ?? raw;
    if (type !== "integer" && type !== "decimal") return raw;
    const n = Number(raw);
    if (!Number.isFinite(n)) return raw;
    const sections = code.split(";");
    const section = n < 0 && sections.length > 1 ? sections[1] : n === 0 && sections.length > 2 ? sections[2] : sections[0];
    const useAbs = n < 0 && sections.length > 1;
    const { tokens, literal } = stripQuoted(section);
    const body = tokens.join("");
    if (/E[+-]0/i.test(body)) return n.toExponential(Math.max(0, (/0\.(0+)/.exec(body)?.[1].length ?? 0))).toUpperCase().replace("E+", "E+").replace(/E([+-])(\d)$/, "E$10$2");
    if (!/[0#]/.test(body)) return (tokens.map((t, i) => (t ? t.replace(/[?,.]/g, "") : literal[i])).join("").replace(/\s+/g, " ").trim()) || raw;   // e.g. the accounting zero section: just "-"
    const percent = body.includes("%");
    const decimals = (/\.([0#?]+)/.exec(body)?.[1].length) ?? 0;
    const thousands = /[0#?],[0#?]/.test(body) || /#,#/.test(body);
    let value = Math.abs(useAbs ? n : n) * (percent ? 100 : 1);
    if (!useAbs && n < 0) value = Math.abs(value);
    let digits = value.toFixed(decimals);
    if (thousands) { const [i, f] = digits.split("."); digits = i.replace(/\B(?=(\d{3})+(?!\d))/g, ",") + (f ? `.${f}` : ""); }
    // Rebuild: replace the numeric placeholder run with the digits, keeping literals and the percent sign.
    let placed = false; let out = "";
    tokens.forEach((t, idx) => {
      if (!t) { out += literal[idx]; return; }
      const rest = t.replace(/[#0?,.]+/g, () => { if (placed) return ""; placed = true; return digits; }).replace(/\((?=)/g, "(").replace(/_/g, "");
      out += rest;
    });
    if (!placed) return raw;
    if (!useAbs && n < 0) out = `-${out}`;
    return out.replace(/\s+/g, " ").trim();
  } catch { return raw; }
}
