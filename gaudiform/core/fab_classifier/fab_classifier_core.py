# -*- coding: utf-8 -*-
"""
FabClassifier core logic — 폴더 내 USD 파일을 FAB별로 분류해 출력 폴더에 복사.

config 예시:
    {
      "input_dir":   "/data/converted",
      "output_dir":  "/data/classified",
      "fab_map": {
        "M15C": ["1st FL", "2nd FL", "3rd FL"],
        "M15D": ["5th FL", "6th FL", "7th FL"]
      },
      "copy_mode": true,
      "unmatched_dir": "_unmatched"
    }
"""

from __future__ import annotations

import os
import shutil

from pxr import Usd, UsdGeom

ATTR_TYPE       = "omni:hoops:metadata:TYPE"
ATTR_LEVEL_NAME = "omni:hoops:metadata:tn__IdentityData_qC:Name"
ATTR_SK_EQ_ID   = "omni:hoops:metadata:tn__IdentityData_qC:SK_EQ_ID"
ATTR_ELEVATION  = "omni:hoops:metadata:Constraints:Elevation"

_TC = Usd.TimeCode.Default()


def _get_attr(prim, attr_name):
    attr = prim.GetAttribute(attr_name)
    if attr and attr.HasValue():
        return attr.Get()
    return None


def _eq_world_z(prim, xf_cache):
    try:
        return xf_cache.GetLocalToWorldTransform(prim).ExtractTranslation()[2]
    except Exception:
        return None


def get_floor_names(stage) -> set[str]:
    """
    1차: IFCBUILDINGSTOREY prim의 world Z xform으로 층 Z 범위 계산, 장비 world Z 검증.
    폴백: 모든 층 world Z 동일(< 0.01m) 시 SK_EQ_ID 첫 등장 층 기준.
    """
    xf_cache = UsdGeom.XformCache(_TC)

    floors = []
    for prim in stage.TraverseAll():
        if _get_attr(prim, ATTR_TYPE) != "IFCBUILDINGSTOREY":
            continue
        name = _get_attr(prim, ATTR_LEVEL_NAME)
        if not name:
            continue
        world_z = _eq_world_z(prim, xf_cache) or 0.0
        floors.append((world_z, str(name).strip(), prim))

    if not floors:
        return set()

    floors.sort(key=lambda x: x[0])
    zs = [f[0] for f in floors]
    all_same = (max(zs) - min(zs)) < 0.01  # 1cm 미만 차이면 동일로 처리

    names: set[str] = set()

    if not all_same:
        for i, (z_min, floor_name, floor_prim) in enumerate(floors):
            z_max = floors[i + 1][0] if i + 1 < len(floors) else float("inf")
            for child in Usd.PrimRange(floor_prim):
                if child == floor_prim:
                    continue
                if not _get_attr(child, ATTR_SK_EQ_ID):
                    continue
                eq_z = _eq_world_z(child, xf_cache)
                if eq_z is not None and z_min <= eq_z < z_max:
                    names.add(floor_name)
                    break
    else:
        seen_eq_ids: set[str] = set()
        for _, floor_name, floor_prim in floors:
            for child in Usd.PrimRange(floor_prim):
                if child == floor_prim:
                    continue
                eq_id = _get_attr(child, ATTR_SK_EQ_ID)
                if not eq_id:
                    continue
                eq_id = str(eq_id).strip()
                if eq_id in seen_eq_ids:
                    continue
                seen_eq_ids.add(eq_id)
                names.add(floor_name)

    return names


def classify_usd(usd_path: str, fab_map: dict[str, list[str]], log=print) -> list[str]:
    """
    USD 파일을 열어 층 이름으로 FAB 분류.
    Returns: 매칭된 FAB 이름 목록 (없으면 빈 리스트)
    """
    try:
        stage = Usd.Stage.Open(usd_path)
    except Exception as e:
        log(f"  [ERROR] 열기 실패 {os.path.basename(usd_path)}: {e}")
        return []

    floor_names = get_floor_names(stage)
    if not floor_names:
        log(f"  [WARN] 층 정보 없음: {os.path.basename(usd_path)}")
        return []

    matched_fabs: list[str] = []
    for fab, floors in fab_map.items():
        floor_set = {f.strip() for f in floors}
        if floor_names & floor_set:
            matched_fabs.append(fab)

    return matched_fabs


def process_folder(
    input_dir: str,
    output_dir: str,
    fab_map: dict[str, list[str]],
    copy_mode: bool = True,
    unmatched_dir: str = "_unmatched",
    log=print,
) -> dict:
    """
    input_dir의 USD 파일을 FAB별로 output_dir/{FAB}/ 에 복사/이동.

    Args:
        input_dir:     USD 파일이 있는 폴더
        output_dir:    출력 루트 폴더
        fab_map:       {"M15C": ["1st FL", ...], "M15D": [...]}
        copy_mode:     True=복사, False=이동
        unmatched_dir: 매칭 안 된 파일을 넣을 서브폴더명

    Returns:
        {"matched": {fab: [files]}, "unmatched": [files], "errors": [files]}
    """
    result = {"matched": {fab: [] for fab in fab_map}, "unmatched": [], "errors": []}

    usd_files = [
        f for f in os.listdir(input_dir)
        if f.lower().endswith((".usd", ".usda", ".usdc", ".usdz"))
    ]
    log(f"[FabClassifier] 총 {len(usd_files)}개 USD 파일 분류 시작")
    log(f"  FAB 목록: {list(fab_map.keys())}")

    op = shutil.copy2 if copy_mode else shutil.move
    op_label = "복사" if copy_mode else "이동"

    for filename in sorted(usd_files):
        src = os.path.join(input_dir, filename)
        fabs = classify_usd(src, fab_map, log=log)

        if not fabs:
            dst_dir = os.path.join(output_dir, unmatched_dir)
            os.makedirs(dst_dir, exist_ok=True)
            op(src, os.path.join(dst_dir, filename))
            result["unmatched"].append(filename)
            log(f"  [UNMATCHED] {filename} → {unmatched_dir}/")
            continue

        for fab in fabs:
            dst_dir = os.path.join(output_dir, fab)
            os.makedirs(dst_dir, exist_ok=True)
            dst = os.path.join(dst_dir, filename)
            op(src, dst)
            result["matched"][fab].append(filename)
            log(f"  [{op_label}] {filename} → {fab}/")

        if len(fabs) > 1:
            log(f"  [WARN] {filename}: 여러 FAB 매칭 {fabs} — 모든 폴더에 복사됨")

    total_matched = sum(len(v) for v in result["matched"].values())
    log(f"[FabClassifier] 완료 — 매칭 {total_matched}건 / 미매칭 {len(result['unmatched'])}건")
    return result
