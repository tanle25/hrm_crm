from __future__ import annotations

import json

from app.graph import run_pipeline
from app.schemas import PipelineState


def main() -> None:
    payload = PipelineState(
        url="https://example.com/bai-viet-mau",
        priority="normal",
        woo_category_id=1,
        focus_keyword_override="noi dung seo",
        publish_status="draft",
    )
    result = run_pipeline("smoke-test", payload.model_dump(by_alias=True))
    print(json.dumps({"status": result["status"], "qa_score": result["qa_result"]["overall_score"], "woo_link": result.get("woo_link")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
