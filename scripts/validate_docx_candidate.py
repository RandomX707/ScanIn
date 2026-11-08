#!/usr/bin/env python3
"""Run offline structural diagnostics on an original/candidate DOCX pair.

This tool never invokes the application release workflow. Rendered PDF inputs,
when supplied, must have been produced by a separately approved safe renderer.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from packages.core.verification.docx_candidate import render_markdown, validate_candidate


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--expected", required=True, type=Path)
    parser.add_argument("--rendered-original-pdf", type=Path,
                        help="PDF rendered by an explicitly approved, isolated DOCX renderer")
    parser.add_argument("--rendered-candidate-pdf", type=Path,
                        help="PDF rendered by the same explicitly approved, isolated DOCX renderer")
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--markdown-out", type=Path)
    args = parser.parse_args()
    if bool(args.rendered_original_pdf) != bool(args.rendered_candidate_pdf):
        parser.error("both rendered PDF evidence files must be supplied together")
    if args.rendered_original_pdf and args.rendered_candidate_pdf:
        if "UNVERIFIED" in args.candidate.name.upper():
            # The name is retained to prevent accidental confusion with released derivatives.
            pass
    try:
        expected = json.loads(args.expected.read_text(encoding="utf-8"))
        report = validate_candidate(
            args.original.read_bytes(), args.candidate.read_bytes(), expected,
            rendered_original_pdf=args.rendered_original_pdf.read_bytes() if args.rendered_original_pdf else None,
            rendered_candidate_pdf=args.rendered_candidate_pdf.read_bytes() if args.rendered_candidate_pdf else None,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"DOCX candidate diagnostics failed to run: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    json_text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    markdown_text = render_markdown(report)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json_text, encoding="utf-8")
    if args.markdown_out:
        args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_out.write_text(markdown_text, encoding="utf-8")
    print(json_text, end="")
    print("\n" + markdown_text, end="")
    if report["structural_status"] != "PASS" or report["visual_status"] == "FAIL":
        return 1
    if report["visual_status"] != "PASS":
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
