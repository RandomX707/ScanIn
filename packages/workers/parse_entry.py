from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from packages.core.adapters.registry import AdapterRegistry


MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_TARGETS = 2000
class ParserRejected(ValueError):
    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


def inspect_bytes(data: bytes, *, filename: str = "", max_expansion_ratio: int = 100,
                  include_targets: bool = False) -> dict[str, Any]:
    digest = hashlib.sha256(data).hexdigest()
    if not data or len(data) > MAX_FILE_BYTES:
        raise ParserRejected("file_size_limit")
    registry = AdapterRegistry()
    selection = registry.inspect(data, filename=filename, max_expansion_ratio=max_expansion_ratio)
    profile = selection.profile
    if profile.level == "rejected":
        reason = profile.reasons[0] if profile.reasons else "invalid_document"
        raise ParserRejected(reason)
    supported = profile.adapter in {"xlsx", "csv", "json"}
    result = {
        "status": "inspected" if supported else "identified",
        "sha256": digest,
        "size_bytes": len(data),
        "media_type": selection.media_type,
        "capability": {"level": "Read-only" if profile.level == "read_only" else profile.level,
                        "reasons": list(profile.reasons)},
        "adapter": profile.adapter,
        "adapter_version": profile.adapter_version,
        "sheets": list(profile.sheets),
        "features": list(profile.features),
        "findings": list(selection.findings),
        "score": selection.score,
    }
    if include_targets and supported:
        source_adapter = {"xlsx": registry.xlsx, "csv": registry.csv, "json": registry.json}[profile.adapter]
        safe_targets = source_adapter.targets(data)
        rejected_targets = source_adapter.unsupported_targets(data)
        all_targets = safe_targets + rejected_targets
        result["targets"] = [_target_dict(target) for target in all_targets[:MAX_TARGETS]]
        result["targets_truncated"] = len(all_targets) > MAX_TARGETS
    return result


def _target_dict(target: Any) -> dict[str, Any]:
    return {
        "id": target.id,
        "sheet": target.sheet,
        "cell": target.cell,
        "label": target.label,
        "value": target.value.canonical(),
        "fingerprint": target.fingerprint,
        "editable": target.editable,
        "reason": target.reason,
        "dependents": list(target.dependents) if target.dependents is not None else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--input", required=True)
    parser.add_argument("--filename", default="")
    parser.add_argument("--targets", action="store_true")
    args = parser.parse_args()
    try:
        path = Path(args.input)
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise ParserRejected("file_size_limit")
        result = inspect_bytes(path.read_bytes(), filename=args.filename or path.name, include_targets=args.targets)
    except ParserRejected as error:
        result = {"status": "rejected", "reason": error.reason_code}
    except (OSError, ValueError):
        result = {"status": "rejected", "reason": "parser_input_unavailable"}
    sys.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
