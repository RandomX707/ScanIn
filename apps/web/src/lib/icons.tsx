const ICONS: Record<string, string> = {
  upload: '<path d="M12 16V3m0 0L7 8m5-5 5 5"/><path d="M4 16v4h16v-4"/>',
  arrow: '<path d="M5 12h14m-6-6 6 6-6 6"/>',
  file: '<rect x="5" y="2" width="14" height="20" rx="2"/><path d="M9 2v5h6"/>',
  layers: '<rect x="4" y="4" width="13" height="13" rx="2"/><path d="M8 8h12v12H8"/>',
  edit: '<path d="M12 5h-7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-7"/><path d="M16 3l5 5-10 10-5 1 1-5L17 4a3 3 0 0 1 4 4"/>',
  check: '<path d="m4 12 5 5L20 6"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v6l4 2"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m16 16 5 5"/>',
  download: '<path d="M12 3v13m-5-5 5 5 5-5M4 18v3h16v-3"/>',
  shield: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z"/><path d="m9 12 2 2 4-4"/>',
  refresh: '<path d="M20 11A8 8 0 0 0 6 6L3 9m0-6v6h6M4 13a8 8 0 0 0 14 5l3-3m0 6v-6h-6"/>',
  chev: '<path d="m9 18 6-6-6-6"/>',
  spark: '<path d="m12 2 2.5 7.5L22 12l-7.5 2.5L12 22l-2.5-7.5L2 12l7.5-2.5L12 2Z"/>',
  close: '<path d="M5 5l14 14M19 5 5 19"/>',
  menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
};

export function Icon({ name, size = 18 }: { name: string; size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"
      dangerouslySetInnerHTML={{ __html: ICONS[name] ?? ICONS.file }} />
  );
}
