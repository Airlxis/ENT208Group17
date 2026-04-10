import json
import re
import difflib
from collections import defaultdict
from pathlib import Path


QA_PATH = Path(__file__).with_name("qa.json")


def norm_q(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[\s\u3000]+", "", s)
    s = s.replace("\uFF1F", "?")  # fullwidth ?
    s = re.sub(r'[\u201c\u201d"\'`]+', "", s)
    s = re.sub(r"[\(\)\uFF08\uFF09\[\]\u3010\u3011]+", "", s)
    s = re.sub(r"[\uFF0C,\u3002\.\uFF1B;\uFF1A:!\uFF01\?\uFF1F]+$", "", s)
    return s


def merge_answers(a1: str, a2: str) -> str:
    a1 = (a1 or "").strip()
    a2 = (a2 or "").strip()
    if not a1:
        return a2
    if not a2:
        return a1
    if a1 == a2:
        return a1
    # prefer the more informative one if one contains the other
    if a1 in a2:
        return a2
    if a2 in a1:
        return a1
    # otherwise concatenate (stable)
    return a1 + "\n" + a2


def dedupe(items: list[dict], near_threshold: float = 0.92) -> tuple[list[dict], dict]:
    # 1) exact duplicates by normalized q
    by_norm: dict[str, int] = {}
    out: list[dict] = []
    exact_merged = 0
    for it in items:
        q = str(it.get("q", "")).strip()
        a = str(it.get("a", "")).strip()
        if not q or not a:
            continue
        nq = norm_q(q)
        if nq in by_norm:
            idx = by_norm[nq]
            out[idx]["a"] = merge_answers(out[idx].get("a", ""), a)
            exact_merged += 1
        else:
            by_norm[nq] = len(out)
            out.append({"q": q, "a": a})

    # 2) near duplicates by SequenceMatcher on normalized q (single-pass greedy)
    norms = [norm_q(it["q"]) for it in out]
    used = set()
    merged: list[dict] = []
    near_groups = 0
    near_merged = 0
    for i in range(len(out)):
        if i in used:
            continue
        base = out[i]
        used.add(i)
        group = [i]
        for j in range(i + 1, len(out)):
            if j in used:
                continue
            if not norms[i] or not norms[j]:
                continue
            r = difflib.SequenceMatcher(None, norms[i], norms[j]).ratio()
            if r >= near_threshold:
                used.add(j)
                group.append(j)
                base["a"] = merge_answers(base.get("a", ""), out[j].get("a", ""))
                # keep the shorter/cleaner question as representative
                if len(base.get("q", "")) > len(out[j].get("q", "")):
                    base["q"] = out[j].get("q", base.get("q", ""))
                near_merged += 1
        if len(group) > 1:
            near_groups += 1
        merged.append(base)

    stats = {
        "input": len(items),
        "after_exact": len(out),
        "exact_merged": exact_merged,
        "after_near": len(merged),
        "near_groups": near_groups,
        "near_merged": near_merged,
        "near_threshold": near_threshold,
    }
    return merged, stats


def main() -> None:
    if not QA_PATH.exists():
        raise SystemExit(f"qa.json not found: {QA_PATH}")
    items = json.loads(QA_PATH.read_text(encoding="utf-8"))
    if not isinstance(items, list):
        raise SystemExit("qa.json must be a list")
    merged, stats = dedupe(items, near_threshold=0.92)
    print("STATS:", json.dumps(stats, ensure_ascii=False))
    # Write back (stable, pretty)
    QA_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"WROTE: {QA_PATH} ({len(merged)} items)")


if __name__ == "__main__":
    main()

