# Visible differences from the prototype that were not removed

- Real data: dates, sizes, folder lists (History shows every folder in the database, not just the demo), and "Saved"/"Original upload" status per real version.
- Footer line, hero-format disclaimer, "Extractive demo" note, edit footnote and Edits description: the prototype's wording ("Interactive product concept", "stay in this browser", "saves an editable HTML derivative") is untrue for the real app, so the sentences were reworded; layout is unchanged.
- "Changes become new versions" replaces "Changes become new drafts" (drafts are not shown in the UI).
- Edits tab: the prototype's rich-text editor is a plain-text editor for the saved format (Markdown/HTML toolbar inserts markup); grid, DOCX and PDF editors and the read-only empty state are new, as required. Save is disabled until something changes; "Export HTML" is "Export <FORMAT>".
- PDF Original preview uses the browser's native PDF frame (blank in headless Chromium screenshots).
- DOCX/XLSX/PPTX extracted previews are built from the sandboxed parser, not mammoth/SheetJS, so spacing of those previews can differ.
