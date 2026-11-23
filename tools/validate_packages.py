# -*- coding: utf-8 -*-
"""校验 packages/*/wmworld.json 是否符合世界接口契约。

用法：
    python tools/validate_packages.py --verse ../satio-verse     # 用本地 clone 的 satio-verse 校验
    python tools/validate_packages.py --verse ../satio-verse --strict  # 任何 warning 也算失败
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--verse", required=True, help="satio-verse 仓库的本地路径（用来 import wm.agent.plugin_manifest）")
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args()

    sys.path.insert(0, str(Path(args.verse).resolve()))
    from wm.agent import plugin_manifest as pm  # noqa: E402

    root = Path(args.root)
    packages = sorted((root / "packages").glob("*/wmworld.json"))
    if not packages:
        print("没有找到 packages/*/wmworld.json")
        return 0

    bad = 0
    for path in packages:
        pkg_dir = path.parent
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {pkg_dir.name}: 读不了 wmworld.json — {exc}")
            bad += 1
            continue

        issues = list(pm.validate_plugin_manifest(manifest, package_dir=pkg_dir))
        issues += list(pm.policy_issues(manifest, "third_party"))
        errors = [i for i in issues if i.level == "error"]
        warns = [i for i in issues if i.level != "error"]

        if manifest.get("id") != pkg_dir.name:
            errors.append(pm.Issue(path="id", code="E_DIR_NAME", message="目录名必须等于 id", level="error"))

        status = "OK" if not errors and not (args.strict and warns) else "FAIL"
        print(f"[{status}] {pkg_dir.name}  errors={len(errors)} warnings={len(warns)}")
        for issue in issues[:20]:
            print(f"    - {issue.level}: {issue.path}: {issue.message} ({issue.code})")
        bad += 0 if status == "OK" else 1

    print(f"\n{len(packages) - bad}/{len(packages)} 个包通过")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
