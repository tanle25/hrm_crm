from __future__ import annotations

import json
from pathlib import Path


def main() -> None:
    legacy_dir = Path("data/v1_exports")
    output_dir = Path("data/v2_migrated")
    output_dir.mkdir(parents=True, exist_ok=True)
    if not legacy_dir.exists():
        print("khong tim thay data/v1_exports, bo qua migrate")
        return

    for path in legacy_dir.glob("*.json"):
        raw = json.loads(path.read_text(encoding="utf-8"))
        upgraded = {
            "url": raw.get("url"),
            "status": raw.get("status", "pending"),
            "metrics": {"total_tokens_used": raw.get("tokens", 0), "estimated_cost_usd": raw.get("cost_usd", 0.0)},
            "plan": raw.get("plan", {}),
        }
        target = output_dir / path.name
        target.write_text(json.dumps(upgraded, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"migrated {path.name}")


if __name__ == "__main__":
    main()
