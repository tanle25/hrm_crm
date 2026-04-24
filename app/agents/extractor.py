from __future__ import annotations

import re
from collections import Counter

from app.llm import call_json


EXTRACTOR_SYSTEM_PROMPT = """
Bạn là Extractor cho nội dung sản phẩm tiếng Việt.
Trả về JSON hợp lệ với các trường:
key_points, important_facts, entities, original_intent, tone, faq_items, steps,
product_components, product_use_cases, product_attributes, buyer_objections,
product_specs, component_profiles.
Giữ cực kỳ ngắn gọn để tránh bị cắt giữa chừng:
- mỗi mảng tối đa 4 phần tử
- description/answer/value ngắn, tối đa 18 từ
- nếu không chắc thì để mảng rỗng hoặc object rỗng
- entities phải là object có keys: people, places, organizations, products
- product_use_cases phải bám đúng bối cảnh dùng thực tế nhìn thấy trong dữ liệu; không mặc định suy diễn sang quà biếu/quà doanh nghiệp nếu dữ liệu không nêu rõ
- buyer_objections phải là băn khoăn mua hàng tổng quát như độ tiện, độ hợp nhu cầu, cảm giác dùng, độ hoàn thiện; không áp văn mẫu riêng cho một loại sản phẩm
- nếu metadata cho biết product_kind là variable, phải ghi nhận các biến thể/quy cách trong product_specs.variants hoặc package_sizes_text
- nếu product_kind là simple, không tự bịa biến thể
Không thêm markdown hay giải thích.
""".strip()


COMPONENT_STOPWORDS = [
    "hộp",
    "bộ quà",
    "set quà",
    "gift set",
    "bao bì",
    "đóng gói",
    "thiệp",
]


def _infer_single_tea(metadata: dict | None, clean_content: str) -> bool:
    metadata = metadata or {}
    title = str(
        metadata.get("title")
        or (metadata.get("product_hints") or {}).get("og_title")
        or ""
    ).lower()
    lower = clean_content.lower()
    if "trà" not in f"{title} {lower}":
        return False
    return not any(token in title for token in ["bộ", "ấm", "hộp", "set", "combo", "quà"])


def _unique(values: list[str], limit: int) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = re.sub(r"\s+", " ", value).strip(" .,:;|-")
        key = cleaned.lower()
        if any(token in key for token in ["trà việt", "giỏ hàng", "đăng nhập", "var ", "yêu thích", "so sánh"]):
            continue
        if len(cleaned) >= 4 and key not in seen:
            seen.add(key)
            output.append(cleaned)
        if len(output) >= limit:
            break
    return output


def _text_from_value(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ["name", "title", "label", "fact", "summary", "description", "text", "profile", "answer", "question"]:
            if isinstance(value.get(key), str):
                return value[key]
        return " ".join(str(item) for item in value.values() if isinstance(item, (str, int, float)))
    if isinstance(value, list):
        return " ".join(str(item) for item in value if isinstance(item, (str, int, float)))
    return "" if value is None else str(value)


def _parse_int(value: object) -> int | None:
    if isinstance(value, int):
        return value
    text = _text_from_value(value)
    match = re.search(r"(\d+)", text)
    return int(match.group(1)) if match else None


def _component_allowed(name: str) -> bool:
    lowered = name.lower()
    if len(name) < 4:
        return False
    if any(stopword in lowered for stopword in COMPONENT_STOPWORDS):
        return False
    return True


def _normalize_components(raw_components: object, hints: dict, fallback: list[str]) -> list[str]:
    hinted = [_text_from_value(item) for item in (hints.get("components") or [])]
    values: list[str] = []
    for item in raw_components or []:
        values.append(_text_from_value(item))
    values.extend(hinted)
    values.extend(fallback)
    normalized = _unique([item for item in values if _component_allowed(_text_from_value(item))], 12)
    return normalized[:8]


def _normalize_profiles(raw_profiles: object, components: list[str], fallback: dict[str, str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    if isinstance(raw_profiles, dict):
        items = raw_profiles.items()
    elif isinstance(raw_profiles, list):
        items = []
        for item in raw_profiles:
            if isinstance(item, dict):
                name = _text_from_value(item.get("name") or item.get("component"))
                if name:
                    items.append((name, item))
    else:
        items = []
    for key, value in items:
        name = _text_from_value(key)
        if not name:
            continue
        matched = next((component for component in components if component.lower() == name.lower()), None)
        if not matched:
            continue
        text = _unique([_text_from_value(value), fallback.get(matched, "")], 1)
        if text:
            normalized[matched] = text[0]
    for component in components:
        if component not in normalized and fallback.get(component):
            normalized[component] = fallback[component]
    return normalized


def _normalize_faq(raw_faq: object, fallback: list[dict]) -> list[dict]:
    faq_items: list[dict] = []
    for item in raw_faq or []:
        if isinstance(item, dict):
            question = _text_from_value(item.get("question"))
            answer = _text_from_value(item.get("answer"))
        else:
            text = _text_from_value(item)
            question, answer = "", text
        if question and answer:
            faq_items.append({"question": question.strip(), "answer": answer.strip()})
    if faq_items:
        return faq_items[:6]
    return fallback


def _normalize_entities(raw_entities: object, components: list[str], fallback: dict) -> dict:
    entities = raw_entities if isinstance(raw_entities, dict) else {}
    normalized = {
        "people": entities.get("people") if isinstance(entities.get("people"), list) else fallback.get("people", []),
        "places": entities.get("places") if isinstance(entities.get("places"), list) else fallback.get("places", []),
        "organizations": entities.get("organizations") if isinstance(entities.get("organizations"), list) else fallback.get("organizations", []),
        "products": components or fallback.get("products", []),
    }
    return normalized


def _normalize_specs(raw_specs: object, components: list[str], fallback: dict) -> dict[str, str | int]:
    raw = raw_specs if isinstance(raw_specs, dict) else {}
    specs: dict[str, str | int] = dict(fallback)

    packets = _parse_int(raw.get("packets_per_box") or raw.get("packets_per_variant") or raw.get("packets_each"))
    grams = _parse_int(raw.get("grams_per_packet") or raw.get("weight_per_packet_grams") or raw.get("grams_each"))
    packaging_text = _text_from_value(raw.get("packaging_per_tea") or raw.get("packaging") or raw.get("packaging_format"))
    if packaging_text:
        packaging_match = re.search(r"(\d+)\s*gói\s*x\s*(\d+)\s*g", packaging_text, re.IGNORECASE)
        if packaging_match:
            packets = packets or int(packaging_match.group(1))
            grams = grams or int(packaging_match.group(2))

    component_count = _parse_int(raw.get("component_count") or raw.get("item_count") or raw.get("variant_count"))
    if components:
        component_count = len(components)

    if packets:
        specs["packets_per_box"] = packets
    if grams:
        specs["grams_per_packet"] = grams
    if component_count:
        specs["component_count"] = component_count

    box_name = _text_from_value(raw.get("box_name") or raw.get("box_model") or raw.get("packaging_name"))
    if box_name and not box_name.strip():
        box_name = ""
    if box_name and box_name.lower() in {component.lower() for component in components}:
        box_name = ""
    if box_name:
        specs["box_name"] = box_name
    box_material = _text_from_value(raw.get("box_material") or raw.get("material"))
    if box_material:
        specs["box_material"] = box_material
    audience_hint = _text_from_value(raw.get("audience_hint") or raw.get("audience") or raw.get("recipient") or raw.get("suitable_for"))
    if audience_hint:
        specs["audience_hint"] = audience_hint

    total_packets = _parse_int(raw.get("total_packets"))
    if packets and component_count:
        total_packets = packets * component_count
    if total_packets:
        specs["total_packets"] = total_packets
    total_weight = _parse_int(raw.get("total_weight_grams") or raw.get("total_weight"))
    if total_packets and grams:
        total_weight = total_packets * grams
    if total_weight:
        specs["total_weight_grams"] = total_weight
    package_sizes = _text_from_value(raw.get("package_sizes_text") or raw.get("package_sizes"))
    if package_sizes:
        specs["package_sizes_text"] = package_sizes
    return specs


def _extract_components(clean_content: str, hints: dict) -> list[str]:
    match = re.search(r"gồm\s+\d+\s+(?:món|loại|thành phần|chi tiết)?\s*:?\s*([^\.]+)", clean_content, re.IGNORECASE)
    if match:
        parts = re.split(r",\s*", match.group(1))
        exact = [part for part in parts if len(part.strip()) >= 4]
        if exact:
            return _unique(exact, 8)
    hinted = [item for item in (hints.get("components") or []) if len(str(item).strip()) >= 4]
    if hinted:
        return _unique(hinted, 8)
    found = re.findall(r"(?:[A-ZÀ-Ỵ][a-zà-ỵ0-9]+(?:\s+[A-ZÀ-Ỵa-zà-ỵ0-9]+){0,5})", clean_content)
    return _unique(found, 8)


def _extract_product_specs(clean_content: str, components: list[str]) -> dict:
    specs: dict[str, str | int] = {}
    box_match = re.search(r"((?:Hộp|Bộ|Ấm|Bình|Ly|Tách)[^,\.]+)", clean_content)
    if box_match:
        specs["box_name"] = box_match.group(1).strip()
    material_match = re.search(r"chất liệu\s+([^\.]+?)(?:\s+Mỗi loại|\s+gồm\s+\d+\s+gói|[.;]|$)", clean_content, re.IGNORECASE)
    if material_match:
        specs["box_material"] = material_match.group(1).strip(" .")
    unit_match = re.search(r"gồm\s+(\d+)\s+(?:gói|món|chi tiết)[^\d]{0,10}(\d+)g", clean_content, re.IGNORECASE)
    if unit_match:
        specs["packets_per_box"] = int(unit_match.group(1))
        specs["grams_per_packet"] = int(unit_match.group(2))
    if components:
        specs["component_count"] = len(components)
    if specs.get("packets_per_box") and specs.get("component_count"):
        specs["total_packets"] = int(specs["packets_per_box"]) * int(specs["component_count"])
    if specs.get("total_packets") and specs.get("grams_per_packet"):
        specs["total_weight_grams"] = int(specs["total_packets"]) * int(specs["grams_per_packet"])
    sizes = re.findall(r"\b(\d{2,4})\s*g\b", clean_content, re.IGNORECASE)
    unique_sizes: list[str] = []
    for size in sizes:
        if size not in unique_sizes:
            unique_sizes.append(size)
    if unique_sizes:
        specs["package_sizes_text"] = ", ".join(f"{size}g" for size in unique_sizes[:5])
    return specs


def _extract_component_profiles(clean_content: str, components: list[str]) -> dict[str, str]:
    sentences = [re.sub(r"\s+", " ", sentence).strip() for sentence in re.split(r"(?<=[.!?])\s+", clean_content) if sentence.strip()]
    profiles: dict[str, list[str]] = {component: [] for component in components}
    current_component = ""
    for sentence in sentences:
        for component in components:
            if sentence.startswith(component):
                current_component = component
                break
        if current_component:
            profiles[current_component].append(sentence)
    normalized: dict[str, str] = {}
    for component, notes in profiles.items():
        if notes:
            normalized[component] = " ".join(notes[:3]).strip()
    return normalized


def _extract_facts(clean_content: str, specs: dict) -> list[str]:
    facts: list[str] = []
    if specs.get("component_count"):
        facts.append(f"Sản phẩm gồm {specs['component_count']} thành phần chính")
    if specs.get("packets_per_box") and specs.get("grams_per_packet"):
        facts.append(f"Quy cách ghi nhận: {specs['packets_per_box']} đơn vị x {specs['grams_per_packet']}g")
    if specs.get("total_weight_grams"):
        facts.append(f"Tổng khối lượng ước tính khoảng {specs['total_weight_grams']}g")
    box_name = specs.get("box_name")
    if box_name:
        facts.append(str(box_name))
    material = specs.get("box_material")
    if material:
        facts.append(f"Chất liệu hộp: {material}")
    package_sizes = specs.get("package_sizes_text")
    if package_sizes:
        facts.append(f"Quy cách phổ biến: {package_sizes}")
    frequent = Counter(re.findall(r"\b[\wÀ-Ỵà-ỵ-]{4,}\b", clean_content.lower()))
    for word, _ in frequent.most_common(8):
        if word not in {fact.lower() for fact in facts}:
            facts.append(word)
    return facts[:10]


def _heuristic_extract(clean_content: str, metadata: dict | None = None) -> dict:
    metadata = metadata or {}
    hints = metadata.get("product_hints") or {}
    sentences = [s.strip() for s in re.split(r"[.!?]\s+", clean_content) if s.strip()]
    key_points = [s for s in sentences[:10] if len(s.split()) >= 5]
    lower = clean_content.lower()

    product_components = _extract_components(clean_content, hints)
    product_specs = _extract_product_specs(clean_content, product_components)
    component_profiles = _extract_component_profiles(clean_content, product_components)
    important_facts = _extract_facts(clean_content, product_specs)
    product_use_cases = []
    if any(term in lower for term in ["gia đình", "tại nhà", "ở nhà", "phòng khách", "bàn trà"]):
        product_use_cases.append("Phù hợp để dùng trong không gian gia đình hoặc góc trà tại nhà")
    if any(term in lower for term in ["văn phòng", "nơi làm việc", "tiếp khách", "công ty"]):
        product_use_cases.append("Phù hợp cho không gian làm việc hoặc tiếp khách nhẹ nhàng")
    if any(term in lower for term in ["biếu", "tặng", "lễ tết", "tri ân", "thăm hỏi"]):
        product_use_cases.append("Có thể cân nhắc cho bối cảnh tặng khi cần sự trang nhã")
    if any(term in lower for term in ["trưng bày", "kệ", "trang trí"]):
        product_use_cases.append("Phù hợp để bày trong không gian sống hoặc góc tiếp khách")

    product_attributes = [keyword for keyword in ["hộp", "bộ", "thủy tinh", "gốm", "giấy gân", "hoa văn sen"] if keyword in lower]
    faq_items = [
        {
            "question": "Sản phẩm gồm những gì?",
            "answer": ", ".join(product_components) if product_components else "Cần kiểm tra trực tiếp thông tin thành phần và phụ kiện đi kèm.",
        },
        {
            "question": "Sản phẩm này phù hợp cho ai?",
            "answer": "; ".join(product_use_cases) if product_use_cases else "Phù hợp cho nhu cầu sử dụng thực tế trong các bối cảnh liên quan.",
        },
        {
            "question": "Thông số hoặc quy cách đáng chú ý là gì?",
            "answer": (
                f"Quy cách ghi nhận: {product_specs['packets_per_box']} đơn vị x {product_specs['grams_per_packet']}g."
                if product_specs.get("packets_per_box") and product_specs.get("grams_per_packet")
                else "Nên kiểm tra trực tiếp thông số và phụ kiện đi kèm trước khi đặt mua."
            ),
        },
    ]
    steps = []
    if any(token in lower for token in ["bước", "buoc", "step", "hướng dẫn", "huong dan", "cách "]):
        steps = [point for point in key_points[:4]]

    return {
        "key_points": key_points or ["Nội dung nguồn cần được bổ sung để phân tích sâu hơn."],
        "important_facts": important_facts,
        "entities": {"people": [], "places": [], "organizations": [], "products": product_components},
        "original_intent": "commercial" if any(t in lower for t in ["mua", "giá", "gia", "sản phẩm", "san pham"]) else "informational",
        "tone": "professional",
        "faq_items": faq_items,
        "steps": steps,
        "product_components": product_components,
        "product_use_cases": product_use_cases,
        "product_attributes": product_attributes,
        "buyer_objections": [
            "Chất liệu và độ hoàn thiện có đúng với kỳ vọng sử dụng hay không",
            "Kích thước, thành phần và cách dùng có hợp nhu cầu thực tế hay không",
            "Hình ảnh và mô tả có phản ánh đúng cảm giác sử dụng thật hay không",
        ],
        "product_specs": product_specs,
        "component_profiles": component_profiles,
    }


def run(clean_content: str, metadata: dict | None = None) -> dict:
    metadata = metadata or {}
    hints = metadata.get("product_hints") or {}
    is_single_tea = _infer_single_tea(metadata, clean_content)
    enriched_content = clean_content
    if hints.get("meta_description"):
        enriched_content = f"{enriched_content}\n\nMeta description: {hints['meta_description']}"
    if hints.get("components"):
        enriched_content = f"{enriched_content}\n\nThành phần phát hiện: {', '.join(hints['components'])}"
    if hints.get("price_text"):
        enriched_content = f"{enriched_content}\n\nGiá hiển thị từ nguồn: {hints['price_text']}"

    fallback = _heuristic_extract(clean_content, metadata)
    prompt = f"Metadata:\n{metadata}\n\nNội dung nguồn:\n{enriched_content[:4200]}"
    data = call_json("extractor", EXTRACTOR_SYSTEM_PROMPT, prompt, fallback=fallback, max_tokens=800)

    product_components = _normalize_components(data.get("product_components"), hints, fallback["product_components"])
    if is_single_tea:
        product_components = []
    data["product_components"] = product_components
    data["product_specs"] = _normalize_specs(data.get("product_specs"), product_components, fallback["product_specs"])
    product_kind = (metadata.get("product_kind") or "").lower()
    if product_kind:
        data["product_specs"]["product_kind"] = product_kind
    variants = hints.get("variants") if isinstance(hints, dict) else []
    if product_kind == "variable" and variants:
        data["product_specs"]["variants"] = variants[:12]
    if is_single_tea:
        package_sizes = re.findall(r"\b(\d{2,4})\s*g\b", clean_content, re.IGNORECASE)
        normalized_sizes: list[str] = []
        for size in package_sizes:
            if size not in normalized_sizes:
                normalized_sizes.append(size)
        if normalized_sizes:
            data["product_specs"]["package_sizes_text"] = ", ".join(f"{size}g" for size in normalized_sizes[:5])
        data["product_specs"].pop("component_count", None)
    data["component_profiles"] = _normalize_profiles(data.get("component_profiles"), product_components, fallback["component_profiles"])
    data["faq_items"] = _normalize_faq(data.get("faq_items"), fallback["faq_items"])
    data["entities"] = _normalize_entities(data.get("entities"), product_components, fallback["entities"])
    if is_single_tea:
        data["entities"]["products"] = []
    data["product_use_cases"] = _unique([_text_from_value(item) for item in (data.get("product_use_cases") or [])] + fallback["product_use_cases"], 6)
    data["product_attributes"] = _unique([_text_from_value(item) for item in (data.get("product_attributes") or [])] + fallback["product_attributes"], 10)
    data["buyer_objections"] = _unique([_text_from_value(item) for item in (data.get("buyer_objections") or [])] + fallback["buyer_objections"], 6)
    data["important_facts"] = _unique([_text_from_value(item) for item in (data.get("important_facts") or [])] + fallback["important_facts"], 10)
    data["key_points"] = _unique([_text_from_value(item) for item in (data.get("key_points") or [])] + fallback["key_points"], 10)
    if not isinstance(data.get("steps"), list):
        data["steps"] = fallback["steps"]
    for key, value in fallback.items():
        data.setdefault(key, value)
    return data
