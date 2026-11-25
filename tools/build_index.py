# -*- coding: utf-8 -*-
"""从 packages/*/*.wmpkg.json 生成 index.json（静态目录，谁都能自建一份）。

设计口径（和 satio-verse 的客户端对齐）：
- sha256 用「规范化 JSON」算：json.dumps(ensure_ascii=False, sort_keys=True, separators=(',', ':'))，和 wm/platform_services.canonical 一致。
- 每个包同时给 `file`（仓库内相对路径，本地源可用）和 `url`（CDN/raw 的 https 地址，静态目录当远程源用）。
- `verified` 由 curated.json 决定：作者在名单里才为 true。它只是「官方认证」标记，
  不代表没认证的不能装 —— 安装闸门永远是客户端的装前报告。
- 输出是确定性的：包按 (id, version) 排序，`updated` 取包文件最新 mtime（可用 --now 覆盖）。

签名（可选）：给 index.json 加 `signature: {alg, key_id, sig}`，签的是
「去掉 signature 和 updated 之后的规范化 JSON」，所以内容没变时签名不会因为重建而失效。
客户端只认内嵌了对应公钥的签名；没有 signature 的第三方目录照常可用（兼容自建源）。

用法：
    python tools/build_index.py                 # 写 index.json
    python tools/build_index.py --check         # 只比对，不一致就 exit 1（CI 用）
    python tools/build_index.py --base-url https://example.com/plugins
    python tools/build_index.py --sign-key tools/keys/catalog-signing.key
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_BASE_URL = "https://cdn.jsdelivr.net/gh/satio-verse/satio-plugins@main"
DEFAULT_CURATED = ["satio-verse"]


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_of(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def version_key(text: str) -> tuple:
    core = text.split("-", 1)[0]
    parts = [int(p) for p in core.split(".") if p.isdigit()]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3]) + ((1,) if "-" in text else (0,),)


def load_curated(root: Path) -> list[str]:
    path = root / "curated.json"
    if not path.is_file():
        return list(DEFAULT_CURATED)
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("authors", [])
    return [str(a) for a in data if isinstance(a, str) and a.strip()]


def load_removed(root: Path) -> list[dict]:
    """removed.json：召回名单。命中就整条从目录里摘掉，客户端拿到 revoked 也会拒绝安装。

    格式：[{"id": "bad-plugin", "sha256": "可选，只召回某个版本", "reason": "为什么"}]
    id 命中 = 该包所有版本召回；id+sha256 = 只召回那一个版本。
    """
    path = root / "removed.json"
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("removed", [])
    out = []
    for row in data if isinstance(data, list) else []:
        if not isinstance(row, dict):
            continue
        pid = str(row.get("id") or "").strip()
        if not pid:
            continue
        item = {"id": pid}
        sha = str(row.get("sha256") or "").strip().lower()
        if sha:
            item["sha256"] = sha
        reason = str(row.get("reason") or "").strip()
        if reason:
            item["reason"] = reason
        out.append(item)
    return sorted(out, key=lambda r: (r["id"], r.get("sha256", "")))


def is_removed(entry: dict, removed: list[dict]) -> bool:
    for row in removed:
        if row["id"] != entry["id"]:
            continue
        if "sha256" in row and row["sha256"] != entry["sha256"]:
            continue
        return True
    return False


def build(root: Path, base_url: str, now: str | None) -> dict:
    curated = load_curated(root)
    removed = load_removed(root)
    packages = []
    newest = 0.0
    for path in sorted((root / "packages").glob("*/*.wmpkg.json")):
        package = json.loads(path.read_text(encoding="utf-8"))
        if package.get("worldmodel_package") != 1:
            raise SystemExit(f"{path}: 不是 worldmodel_package=1 的包")
        spec = package.get("spec") or {}
        pid = spec.get("id") or path.parent.name
        version = package.get("version") or ""
        rel = path.relative_to(root).as_posix()
        entry = {
            "id": pid,
            "name": spec.get("name") or pid,
            "description": spec.get("description") or "",
            "author": package.get("author") or "unknown",
            "version": version,
            "sha256": sha256_of(package),
            "file": rel,
            "url": base_url.rstrip("/") + "/" + rel,
            "verified": bool(package.get("author") in curated),
        }
        if spec.get("kind"):
            entry["kind"] = spec["kind"]
        if package.get("license"):
            entry["license"] = package["license"]
        if package.get("homepage"):
            entry["homepage"] = package["homepage"]
        newest = max(newest, path.stat().st_mtime)
        if is_removed(entry, removed):
            continue
        packages.append(entry)

    packages.sort(key=lambda e: (e["id"], version_key(e["version"])))
    stamp = now or datetime.fromtimestamp(newest or 0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest = {
        "worldmodel_index": 1,
        "catalog_version": 2,
        "updated": stamp,
        "verified_authors": curated,
        "note": "插件包由 PR 提交，CI 校验后合并；这一份是静态目录，客户端「市场」页直接读它。",
        "revoked": removed,
        "packages": packages,
    }
    return manifest


SIGNATURE_ALG = "ed25519"
DEFAULT_KEY_ID = "satio-catalog-1"
UNSIGNED_FIELDS = ("signature", "updated")


def signing_payload(manifest: dict) -> bytes:
    """签名口径：去掉 signature 与 updated 后的规范化 JSON。"""
    return canonical({k: v for k, v in manifest.items() if k not in UNSIGNED_FIELDS})


def sign_manifest(manifest: dict, key_path: Path, key_id: str = DEFAULT_KEY_ID) -> dict:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit(f"{key_path}: 不是 Ed25519 私钥")
    signature = key.sign(signing_payload(manifest))
    signed = dict(manifest)
    signed["signature"] = {
        "alg": SIGNATURE_ALG,
        "key_id": key_id,
        "sig": base64.b64encode(signature).decode("ascii"),
    }
    return signed


def public_key_b64(key_path: Path) -> str:
    """从私钥导出公钥（base64 raw），方便贴进客户端常量。"""
    from cryptography.hazmat.primitives import serialization

    key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    raw = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return base64.b64encode(raw).decode("ascii")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ap.add_argument("--now", default="", help="固定 updated，便于复现")
    ap.add_argument("--check", action="store_true", help="只比对，不写盘")
    ap.add_argument("--sign-key", default="", help="Ed25519 私钥 PEM；给了才给 index 签名")
    ap.add_argument("--key-id", default=DEFAULT_KEY_ID, help="写进 signature.key_id，客户端据此选公钥")
    ap.add_argument("--show-pubkey", action="store_true", help="只打印公钥 base64 后退出")
    args = ap.parse_args()

    root = Path(args.root)
    target = root / "index.json"
    if args.show_pubkey:
        if not args.sign_key:
            print("--show-pubkey 需要 --sign-key")
            return 2
        print(public_key_b64(Path(args.sign_key)))
        return 0
    built = build(root, args.base_url, args.now or None)
    if args.sign_key:
        built = sign_manifest(built, Path(args.sign_key), args.key_id)
    text = json.dumps(built, ensure_ascii=False, indent=2) + "\n"

    if args.check:
        if not target.is_file():
            print("index.json 不存在")
            return 1
        current = json.loads(target.read_text(encoding="utf-8"))
        probe = json.loads(text)
        # updated 与 signature 允许漂移：只比目录内容
        for item in (current, probe):
            item.pop("updated", None)
            item.pop("signature", None)
        if current != probe:
            print("index.json 与 packages/ 不一致，请运行 python tools/build_index.py")
            return 1
        mark = "已签名" if "signature" in json.loads(target.read_text(encoding="utf-8")) else "未签名"
        print(f"index ok: {len(built['packages'])} 个包（{mark}）")
        return 0

    target.write_text(text, encoding="utf-8", newline="\n")
    print(f"写好了 {target}：{len(built['packages'])} 个包，verified={sum(1 for p in built['packages'] if p['verified'])}")
    for entry in built["packages"]:
        mark = "认证" if entry["verified"] else "社区"
        print(f"  [{mark}] {entry['id']} {entry['version']}  {entry['sha256'][:12]}…")
    return 0


if __name__ == "__main__":
    sys.exit(main())
