from __future__ import annotations

from app.chroma import add_document

SEED_DOCUMENTS = [
    {
        "id": "seed_1",
        "title": "Huong dan danh gia noi dung SEO",
        "content": "Noi dung chat luong can co cau truc ro rang, nguon dan chieu va tra loi truc tiep nhu cau nguoi doc.",
        "metadata": {"source": "seed://seo-guide", "type": "seed", "status": "published", "title": "Huong dan danh gia noi dung SEO", "keywords": "seo,huong dan"},
    },
    {
        "id": "seed_2",
        "title": "Toi uu FAQ cho GEO",
        "content": "FAQ nen ngam tra loi cac cau hoi that su duoc tim kiem va giu cau tra loi ngan, ro, de lay doan trich dan.",
        "metadata": {"source": "seed://geo-faq", "type": "seed", "status": "published", "title": "Toi uu FAQ cho GEO", "keywords": "geo,faq"},
    },
]


def main() -> None:
    for item in SEED_DOCUMENTS:
        add_document(item["content"], item["metadata"], item["id"])
        print(f"seeded {item['id']}: {item['title']}")


if __name__ == "__main__":
    main()
