from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Sequence

from .models import Business

COLUMNS = [
    "id", "name", "category", "city", "address", "phone", "email", "email_valid", "website",
    "instagram", "facebook", "telegram", "other_socials",
    "has_automation", "automation", "size_estimate",
    "contact_score", "status", "source", "osm_id", "merged_ids",
    "lat", "lon", "last_verified_at", "notes",
]

PRIMARY_SOCIALS = ("instagram", "facebook", "telegram")


def row_dict(biz: Business) -> dict:
    socials = biz.socials or {}
    other = {k: v for k, v in socials.items() if k not in PRIMARY_SOCIALS}
    return {
        "id": biz.id,
        "name": biz.name,
        "category": biz.category,
        "city": biz.city,
        "address": biz.address or "",
        "phone": biz.phone or "",
        "email": biz.email or "",
        "email_valid": "" if biz.email_valid is None else int(biz.email_valid),
        "website": biz.website or "",
        "instagram": socials.get("instagram", ""),
        "facebook": socials.get("facebook", ""),
        "telegram": socials.get("telegram", ""),
        "other_socials": ", ".join(f"{k}: {v}" for k, v in other.items()),
        "has_automation": "" if biz.has_automation is None else int(biz.has_automation),
        "automation": biz.automation_summary,
        "size_estimate": biz.size_estimate or "",
        "contact_score": biz.contact_score,
        "status": biz.status,
        "source": biz.source,
        "osm_id": biz.osm_id,
        "merged_ids": ", ".join(biz.merged_ids or []),
        "lat": biz.lat or "",
        "lon": biz.lon or "",
        "last_verified_at": biz.last_verified_at.isoformat() if biz.last_verified_at else "",
        "notes": (biz.notes or "").replace("\n", " ").strip(),
    }


def to_csv(rows: Sequence[Business], path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig — чтобы Excel не превращал кириллицу в кракозябры
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        for biz in rows:
            writer.writerow(row_dict(biz))
    return len(rows)


def to_json(rows: Sequence[Business], path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [row_dict(biz) for biz in rows]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(rows)
