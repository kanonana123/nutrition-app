import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DB_FILE = DATA_DIR / "menu_db.json"
LOG_FILE = DATA_DIR / "migration_log.json"
BACKUP_DIR = DATA_DIR / "backups"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_text(v: Any) -> str:
    if v is None:
        return ""
    return str(v).replace("\ufeff", "").strip()


def parse_price(v: Any) -> Tuple[Any, Any]:
    if isinstance(v, (int, float)):
        return float(v), None
    s = normalize_text(v)
    if not s:
        return None, None
    digits = ""
    dot_used = False
    for ch in s:
        if ch.isdigit():
            digits += ch
        elif ch == "." and not dot_used:
            digits += ch
            dot_used = True
        elif digits:
            break
    price = float(digits) if digits else None
    unit = s.split("/", 1)[1].strip() if "/" in s else None
    return price, unit


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def dish_name(item: Any) -> str:
    if isinstance(item, str):
        return normalize_text(item)
    if not isinstance(item, dict):
        return ""
    return normalize_text(item.get("菜品名称") or item.get("名称") or item.get("name"))


def normalize_dish(canteen: str, window_name: str, category: str, item: Any, source_file: str) -> Dict[str, Any]:
    name = dish_name(item)
    raw_price = item.get("价格") if isinstance(item, dict) else None
    if isinstance(item, dict) and raw_price is None:
        raw_price = item.get("price")
    price, unit = parse_price(raw_price)
    key = f"{canteen}::{window_name}::{name}"
    return {
        "key": key,
        "name": name,
        "price": price,
        "window_id": None,
        "window_name": window_name,
        "canteen_id": canteen,
        "category": category or None,
        "nutrition_info": None,
        "meal_period": "午餐/晚餐",
        "store_name": f"{canteen} · {window_name}" + (f" · {category}" if category else ""),
        "unit": unit,
        "source_file": source_file,
    }


def parse_canteen_stalls(canteen: str, data: Dict[str, Any], source_file: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    stalls = data.get("档口列表")
    if not isinstance(stalls, list):
        return [], ["档口列表字段缺失或非数组"]

    dishes: List[Dict[str, Any]] = []
    errors: List[str] = []
    for idx, stall in enumerate(stalls):
        if not isinstance(stall, dict):
            errors.append(f"第{idx + 1}个档口结构非法")
            continue
        window_name = normalize_text(stall.get("档口名称")) or f"档口{idx + 1}"
        for k, v in stall.items():
            if k in ("档口名称", "基础信息", "备注"):
                continue
            category = normalize_text(k)
            if isinstance(v, list):
                for row in v:
                    d = normalize_dish(canteen, window_name, category, row, source_file)
                    if not d["name"]:
                        errors.append(f"{window_name}/{category} 存在空菜名条目")
                        continue
                    dishes.append(d)
                continue
            if isinstance(v, dict):
                for name, price in v.items():
                    row = {"名称": name, "价格": price}
                    d = normalize_dish(canteen, window_name, category, row, source_file)
                    if not d["name"]:
                        errors.append(f"{window_name}/{category} 存在空菜名对象")
                        continue
                    dishes.append(d)
                continue
            if isinstance(v, str):
                # 忽略备注类字符串
                continue
    return dishes, errors


def parse_source(path: Path, canteen: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    data = read_json(path)
    if isinstance(data, dict) and isinstance(data.get("档口列表"), list):
        return parse_canteen_stalls(canteen, data, path.name)
    return [], [f"{path.name} 暂不支持该格式，仅支持含档口列表的菜单"]


def validate_dishes(dishes: List[Dict[str, Any]]) -> List[str]:
    errors: List[str] = []
    for d in dishes:
        if not d.get("key"):
            errors.append("缺失key")
        if not d.get("name"):
            errors.append(f"{d.get('window_name', '?')} 缺失菜品名称")
        if not d.get("canteen_id"):
            errors.append(f"{d.get('name', '?')} 缺失食堂标识")
        if not d.get("window_name"):
            errors.append(f"{d.get('name', '?')} 缺失窗口名称")
    return errors


def ensure_db() -> Dict[str, Any]:
    if DB_FILE.exists():
        return read_json(DB_FILE)
    return {"schema_version": 1, "updated_at": now_iso(), "dishes": []}


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def backup_db() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_path = BACKUP_DIR / f"menu_db-{ts}.json"
    if DB_FILE.exists():
        shutil.copy2(DB_FILE, backup_path)
    else:
        save_json(backup_path, {"schema_version": 1, "updated_at": now_iso(), "dishes": []})
    return backup_path


def append_log(entry: Dict[str, Any]) -> None:
    data = []
    if LOG_FILE.exists():
        old = read_json(LOG_FILE)
        if isinstance(old, list):
            data = old
    data.append(entry)
    save_json(LOG_FILE, data)


def import_menu(source: Path, canteen: str) -> int:
    if not source.exists():
        raise FileNotFoundError(f"找不到源文件: {source}")
    dishes, parse_errors = parse_source(source, canteen)
    validate_errors = validate_dishes(dishes)
    all_errors = parse_errors + validate_errors

    db = ensure_db()
    existing = {d.get("key"): d for d in db.get("dishes", []) if isinstance(d, dict)}
    inserted = 0
    updated = 0
    for d in dishes:
        key = d["key"]
        if key in existing:
            prev = existing[key]
            merged = {**prev, **d}
            if prev.get("price") is not None and d.get("price") is None:
                merged["price"] = prev.get("price")
            existing[key] = merged
            updated += 1
        else:
            existing[key] = d
            inserted += 1

    backup_path = backup_db()
    db["schema_version"] = 1
    db["updated_at"] = now_iso()
    db["dishes"] = list(existing.values())
    save_json(DB_FILE, db)

    append_log(
        {
            "id": f"import-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
            "timestamp": now_iso(),
            "source_file": str(source),
            "canteen_id": canteen,
            "inserted": inserted,
            "updated": updated,
            "errors": len(all_errors),
            "error_items": all_errors[:100],
            "backup_file": str(backup_path),
        }
    )
    print(f"导入完成: 新增 {inserted}, 更新 {updated}, 错误 {len(all_errors)}")
    print(f"备份文件: {backup_path}")
    return 0 if not all_errors else 2


def rollback(backup_file: str) -> int:
    if backup_file == "latest":
        candidates = sorted(BACKUP_DIR.glob("menu_db-*.json"))
        if not candidates:
            raise FileNotFoundError("未找到可回滚备份")
        source = candidates[-1]
    else:
        source = Path(backup_file)
    if not source.exists():
        raise FileNotFoundError(f"备份文件不存在: {source}")

    current_backup = backup_db()
    shutil.copy2(source, DB_FILE)
    append_log(
        {
            "id": f"rollback-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
            "timestamp": now_iso(),
            "action": "rollback",
            "from_backup": str(source),
            "pre_rollback_backup": str(current_backup),
        }
    )
    print(f"回滚完成: {source} -> {DB_FILE}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="菜单数据迁移脚本（含备份/回滚）")
    parser.add_argument("--source", default=str(ROOT / "二食堂.json"), help="待导入源文件路径")
    parser.add_argument("--canteen", default="二食堂", help="食堂标识")
    parser.add_argument("--rollback", default=None, help="回滚：latest 或指定备份文件")
    args = parser.parse_args()

    if args.rollback:
        return rollback(args.rollback)
    return import_menu(Path(args.source), args.canteen)


if __name__ == "__main__":
    raise SystemExit(main())
