"""把 ORCID 公共 API 的公开成果同步成 data/publications.js（页面运行时兜底数据）。

用法：
    python sync-orcid.py                       # 用默认 iD
    python sync-orcid.py 0009-0009-2390-4129   # 指定 iD

设计要点：
1. 免密钥、免鉴权，只读 ORCID 上标记为 public 的数据。
2. 先拉 /works 列表，再逐条拉 /work/{put-code} 拿作者列表和摘要——列表接口不返回这两项。
3. data/publications.js 里的 extras 段是人工补充的、没有 DOI 的论文（比如中文期刊），
   脚本会原样保留，不会被覆盖。
"""

import json
import re
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_ORCID = "0009-0009-2390-4129"
# 用于在作者列表里定位本人，支持多个别名
OWNER_NAMES = ["Tianning Wang", "王天宁", "T. Wang"]

ORCID_ID = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ORCID
BASE = f"https://pub.orcid.org/v3.0/{ORCID_ID}"

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "publications.js"


def fetch(path: str) -> dict:
    req = urllib.request.Request(f"{BASE}{path}", headers={"Accept": "application/json"})
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
        return json.loads(r.read().decode("utf-8"))


def dig(obj, *keys, default=None):
    cur = obj
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return default if cur is None else cur


def clean_text(s: str) -> str:
    """ORCID 的摘要带 jats / html 标签，去掉标签并压平空白。"""
    if not s:
        return ""
    s = re.sub(r"</?jats:[a-z]+[^>]*>", " ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"&lt;", "<", s)
    s = re.sub(r"&gt;", ">", s)
    s = re.sub(r"&amp;", "&", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def pick_doi(work: dict):
    ids = dig(work, "external-ids", "external-id", default=[]) or []
    fallback = None
    for item in ids:
        if dig(item, "external-id-type", default="").lower() != "doi":
            continue
        value = dig(item, "external-id-value")
        entry = {
            "doi": value,
            "url": dig(item, "external-id-url", "value") or (f"https://doi.org/{value}" if value else None),
        }
        if dig(item, "external-id-relationship") == "self":
            return entry
        fallback = fallback or entry
    return fallback


def parse_year(work: dict):
    try:
        return int(dig(work, "publication-date", "year", "value"))
    except (TypeError, ValueError):
        return None


def author_position(names: list[str]):
    """返回 (第几作者, 共几作者)；找不到本人返回 (None, 总数)。"""
    total = len(names)
    for i, n in enumerate(names, start=1):
        if any(o.lower() in n.lower() or n.lower() in o.lower() for o in OWNER_NAMES):
            return i, total
    return None, total


def main():
    print(f"[1/4] 拉取成果列表 {BASE}/works")
    data = fetch("/works")

    groups = data.get("group", [])
    print(f"[2/4] 列表含 {len(groups)} 条，逐条拉取详情（作者 / 摘要）")

    works = []
    skipped = 0
    for grp in groups:
        summaries = sorted(
            grp.get("work-summary", []) or [],
            key=lambda s: int(dig(s, "display-index", default="0") or 0),
        )
        if not summaries:
            skipped += 1
            continue
        head = summaries[0]
        if dig(head, "visibility", default="public") != "public":
            skipped += 1
            continue

        put_code = head.get("put-code")
        detail = head
        if put_code:
            try:
                detail = fetch(f"/work/{put_code}")
            except (urllib.error.URLError, TimeoutError) as e:
                print(f"      详情拉取失败 put-code={put_code}：{e}，退回列表数据")

        names = [
            dig(c, "credit-name", "value")
            for c in (dig(detail, "contributors", "contributor", default=[]) or [])
            if dig(c, "credit-name", "value")
        ]
        pos, total = author_position(names)
        doi = pick_doi(detail) or pick_doi(head)

        works.append(
            {
                "title": dig(detail, "title", "title", "value", default="(untitled)"),
                "journal": dig(detail, "journal-title", "value"),
                "type": dig(detail, "type", default="journal-article"),
                "year": parse_year(detail) or parse_year(head),
                "date": "-".join(
                    p for p in [
                        dig(detail, "publication-date", "year", "value"),
                        dig(detail, "publication-date", "month", "value"),
                        dig(detail, "publication-date", "day", "value"),
                    ] if p
                ),
                "doi": (doi or {}).get("doi"),
                "url": (doi or {}).get("url") or dig(detail, "url", "value") or dig(head, "url", "value"),
                "authors": names,
                "authorPosition": pos,
                "authorCount": total,
                "abstract": clean_text(dig(detail, "short-description")),
            }
        )

    works.sort(key=lambda x: (x["year"] or 0, x["date"] or ""), reverse=True)
    print(f"[3/4] 解析出 {len(works)} 条公开成果" + (f"（跳过 {skipped} 条非公开）" if skipped else ""))

    # 保留上一次人工补充的 extras（没有 DOI、进不了 ORCID 的论文）
    extras = []
    if OUT.exists():
        m = re.search(r'"extras"\s*:\s*(\[.*?\n  \])', OUT.read_text(encoding="utf-8"), re.S)
        if m:
            try:
                extras = json.loads(m.group(1))
                print(f"      保留人工补充 {len(extras)} 条")
            except json.JSONDecodeError:
                pass

    payload = {
        "orcid": ORCID_ID,
        "ownerNames": OWNER_NAMES,
        "syncedAt": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "works": works,
        "extras": extras,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n"：强制写 LF。Windows 上 Path.write_text 默认会把 \n 翻成 \r\n，
    # 会让生成的文件和其他源码换行符不一致（参见仓库根目录的 .gitattributes）。
    OUT.write_text(
        "/* 由 scripts/sync-orcid.py 自动生成。重新生成会覆盖 works 段，但 extras 段会被保留。\n"
        f"   重新拉取：python scripts/sync-orcid.py {ORCID_ID} */\n"
        f"window.PUBLICATIONS = {json.dumps(payload, ensure_ascii=False, indent=2)};\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"[4/4] 已写入 {OUT}\n")

    for w in works:
        pos = f"第{w['authorPosition']}作者" if w["authorPosition"] else "作者位次未知"
        print(f"  {w['year']} | {w['journal']} | {pos}/{w['authorCount']}人 | {w['title'][:52]}")


if __name__ == "__main__":
    main()
