# -*- coding: utf-8 -*-
"""
FabSplitter core logic — USD stage를 배관류(_util)와 층별 파일로 분리.
pxr 단독으로 동작 (Kit/Omniverse 불필요).
"""

from __future__ import annotations

import gc
import os
import re
import shutil

from pxr import Sdf, Usd, UsdGeom

# ── Metadata attribute names (Hoops Connector 규칙) ───────────────────────────

ATTR_CATEGORY   = "omni:hoops:metadata:Other:Category"
ATTR_LEVEL_NAME = "omni:hoops:metadata:tn__IdentityData_qC:Name"
ATTR_TYPE       = "omni:hoops:metadata:TYPE"

# ── Default config ─────────────────────────────────────────────────────────────

DEFAULT_CFG = {
    "util_categories":            ["Pipes", "Pipe Fittings", "Pipe Accessories", "Flex Pipes"],
    "equipment_categories":       ["Mechanical Equipment"],   # 층별 분류 대상 카테고리
    "output_ext":                 ".usd",
    "prototype_scope_names":      ["Prototypes"],
    "subfolder_per_file":         True,
    "normalize_level_name":       False,   # True 시 층 이름 정규화 (예: "9th FL" → "9F")
    "suffix_sep":                 "@",     # 파일명 구분자: {basename}@{suffix}.usd
    "floor_classify_by_z":        True,    # True: Z 기반 층 분류 / False: 부모 계층 기반
    "floor_z_use_bbox_min":       False,  # True: bbox Z min(장비 바닥) 기준 / False: prim origin Z 기준
    "floor_z_boundary_tolerance": 0.01,  # Z기반 분류 시 층 Z origin과의 snap 허용 오차(m)
    "debug_log":                  False,  # True: {파일명}_fab_split_debug.log 생성
}

_UNSAFE_FILENAME_RE  = re.compile(r'[\\/:*?"<>|₩]')
_FLOOR_NORMALIZE_RE  = re.compile(
    r'^(B?\d+)\s*(?:ST|ND|RD|TH)?\s*(?:FL(?:OOR)?|F|층)?$',
    re.IGNORECASE,
)


def _sanitize_filename(s: str) -> str:
    return _UNSAFE_FILENAME_RE.sub('_', s)


def _normalize_level_name(name: str) -> str:
    """층 이름 정규화. 예) '9th FL' → '9F', 'B1 FL' → 'B1F', '1층' → '1F'"""
    s = name.strip()
    m = _FLOOR_NORMALIZE_RE.match(s)
    if m:
        return f"{m.group(1).upper()}F"
    return s


def _get_attr(prim, attr_name):
    attr = prim.GetAttribute(attr_name)
    if attr and attr.HasValue():
        return attr.Get()
    return None


def _build_floor_z_table(stage, log=None):
    """IFCBUILDINGSTOREY 프림의 월드 Z 좌표로 층 Z-범위 테이블 구성.
    Returns:
        list of (z_min, z_max, level_name) sorted ascending.
        마지막 층의 z_max = float('inf').
    """
    seen_names: dict = {}  # level_name → world_z (첫 등장만)
    for prim in stage.TraverseAll():
        if _get_attr(prim, ATTR_TYPE) != "IFCBUILDINGSTOREY":
            continue
        level_name = _get_attr(prim, ATTR_LEVEL_NAME) or ""
        if not level_name or level_name in seen_names:
            continue
        try:
            mat = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
            seen_names[level_name] = mat.ExtractTranslation()[2]
        except Exception:
            pass

    if not seen_names:
        return []

    floors = sorted(seen_names.items(), key=lambda x: x[1])  # (name, z) by z
    if log:
        for name, z in floors:
            log(f"  [FLOOR_Z] {name!r}: world_z={z:.4f}")
    result = []
    for i, (name, z) in enumerate(floors):
        z_max = floors[i + 1][1] if i + 1 < len(floors) else float('inf')
        result.append((z, z_max, name))
    return result


def _get_prim_origin_z(prim) -> float | None:
    """prim의 world Z origin 반환."""
    try:
        mat = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
        return mat.ExtractTranslation()[2]
    except Exception:
        return None


def _get_bbox_world_range(prim, bbox_cache) -> tuple[float, float] | None:
    """bbox world Z (min, max) 반환.

    ComputeWorldBound가 prototype 로컬 공간을 반환하는 경우
    prim origin world Z를 합산해 world 공간으로 변환한다.
    """
    bbox_target = prim
    if prim.IsInstanceProxy():
        inst_root = _find_instance_root(prim)
        if inst_root is not None:
            bbox_target = inst_root
    try:
        rng = bbox_cache.ComputeWorldBound(bbox_target).GetRange()
        if rng.IsEmpty():
            return None
        w_z_min = float(rng.GetMin()[2])
        w_z_max = float(rng.GetMax()[2])
        prim_z = _get_prim_origin_z(prim) or 0.0
        # prototype 로컬 감지: w_z_min과 prim_z 차이가 w_z_min 자체보다 크면 로컬 공간
        if abs(w_z_min - prim_z) >= abs(w_z_min):
            w_z_min += prim_z
            w_z_max += prim_z
        return (w_z_min, w_z_max)
    except Exception:
        return None


def _classify_floor_by_z(prim, floor_z_table, boundary_tol: float = 0.0, bbox_cache=None, log=None):
    """장비 프림의 Z 기준으로 층 이름 반환. 매칭 안 되면 None.

    bbox_cache 가 있으면 bbox world Z 범위와 각 층 범위의 겹치는 길이가 가장 큰 층 반환.
    bbox가 없으면 prim origin world Z로 범위 기반 분류 (boundary_tol 적용).
    """
    if not floor_z_table:
        return None

    # ── bbox 겹침 기반 분류 ──────────────────────────────────────────────────
    if bbox_cache is not None:
        bbox_range = _get_bbox_world_range(prim, bbox_cache)
        if bbox_range is not None:
            bz_min, bz_max = bbox_range
            if log:
                log(f"  [BBOX_Z] {prim.GetPath()} world_z=({bz_min:.4f},{bz_max:.4f})")
            best_name: str | None = None
            best_overlap = -1.0
            for f_min, f_max, name in floor_z_table:
                overlap = max(0.0, min(bz_max, f_max) - max(bz_min, f_min))
                if overlap > best_overlap:
                    best_overlap = overlap
                    best_name = name
            if log and best_name:
                log(f"  [FLOOR] → {best_name!r} (overlap={best_overlap:.4f}m)")
            return best_name

    # ── prim origin Z 기반 분류 (fallback) ─────────────────────────────────
    z = _get_prim_origin_z(prim)
    if z is None:
        return None
    if log:
        log(f"  [ORIGIN_Z] {prim.GetPath()} z={z:.4f}")

    if boundary_tol > 0:
        candidates = [
            (abs(z - t[0]), t[2])
            for t in floor_z_table
            if abs(z - t[0]) <= boundary_tol
        ]
        if candidates:
            return min(candidates, key=lambda x: x[0])[1]

    for z_min, z_max, name in floor_z_table:
        if z_min <= z < z_max:
            return name
    if z < floor_z_table[0][0]:
        return floor_z_table[0][2]
    return floor_z_table[-1][2]


def _level_ancestor(prim):
    """부모 계층에서 가장 가까운 IFCBUILDINGSTOREY 프림 반환."""
    current = prim.GetParent()
    while current and current.GetPath() != Sdf.Path("/"):
        if _get_attr(current, ATTR_TYPE) == "IFCBUILDINGSTOREY":
            return current
        current = current.GetParent()
    return None


def _find_instance_root(prim):
    """instance proxy 프림에서 실제 instance prim(IsInstance=True) 반환."""
    current = prim
    while current.IsValid() and not current.IsPseudoRoot():
        if current.IsInstance():
            return current
        current = current.GetParent()
    return None


# ── Collection ─────────────────────────────────────────────────────────────────

def collect_by_util_and_floor(stage, cfg, log=print):
    """
    Returns:
        util_paths:    list[SdfPath] — util_categories에 속하는 컴포넌트 (배관류)
        floor_dict:    dict[str, list[SdfPath]] — equipment_categories, 층별 분류
        no_level_paths: list[SdfPath] — equipment_categories이지만 층 미분류
        other_paths:   list[SdfPath] — util도 equipment도 아닌 나머지 컴포넌트
    """
    util_cat_set  = set(cfg.get("util_categories", []))
    equip_cat_set = set(cfg.get("equipment_categories", ["Mechanical Equipment"]))
    do_normalize  = cfg.get("normalize_level_name", False)
    classify_by_z = cfg.get("floor_classify_by_z", True)
    util_paths: list  = []
    floor_dict: dict  = {}
    no_level_paths: list = []
    other_paths: list = []
    seen: set = set()
    total = 0

    boundary_tol  = float(cfg.get("floor_z_boundary_tolerance", 0.0))
    use_bbox_min  = cfg.get("floor_z_use_bbox_min", False)

    if classify_by_z:
        floor_z_table = _build_floor_z_table(stage, log=log)
        mode_desc = "bbox_min" if use_bbox_min else "origin"
        log(f"  Floors detected (Z-order): {[t[2] for t in floor_z_table]}"
            + f" [z_mode={mode_desc}"
            + (f", boundary_tol={boundary_tol}m" if boundary_tol > 0 else "")
            + "]")
        if use_bbox_min:
            bbox_cache = UsdGeom.BBoxCache(
                Usd.TimeCode.Default(), ["default", "render"], useExtentsHint=False
            )
        else:
            bbox_cache = None
    else:
        floor_z_table = None
        bbox_cache    = None
        log("  Floor classify mode: parent hierarchy")

    for prim in stage.TraverseAll():
        if Usd.ModelAPI(prim).GetKind() != "component":
            continue
        total += 1

        # instance proxy인 경우 → instance root 경로로 export
        if prim.IsInstanceProxy():
            inst_root = _find_instance_root(prim)
            if inst_root is None:
                continue
            export_path = inst_root.GetPath()
        else:
            export_path = prim.GetPath()

        # 같은 instance root가 중복 수집되지 않도록
        if export_path in seen:
            continue
        seen.add(export_path)

        # 메타데이터는 proxy에서 읽기 (attribute 상속 지원)
        cat = _get_attr(prim, ATTR_CATEGORY)
        if cat in util_cat_set:
            util_paths.append(export_path)
        elif cat in equip_cat_set:
            if classify_by_z:
                level_name = _classify_floor_by_z(prim, floor_z_table, boundary_tol=boundary_tol, bbox_cache=bbox_cache, log=log)
            else:
                level_prim = _level_ancestor(prim)
                level_name = (_get_attr(level_prim, ATTR_LEVEL_NAME) or "") if level_prim else ""
                level_name = level_name or None
            if level_name:
                if do_normalize:
                    level_name = _normalize_level_name(level_name)
                floor_dict.setdefault(level_name, []).append(export_path)
            else:
                no_level_paths.append(export_path)
        else:
            other_paths.append(export_path)

    log(f"  Collection: total={total}, util={len(util_paths)}, "
        f"floors={sorted(floor_dict.keys())}, no_level={len(no_level_paths)}, "
        f"other={len(other_paths)}")
    return util_paths, floor_dict, no_level_paths, other_paths


# ── Export helpers ─────────────────────────────────────────────────────────────

def _copy_stage_metadata(src_layer, dst_layer) -> None:
    dst_layer.defaultPrim   = src_layer.defaultPrim
    dst_layer.documentation = src_layer.documentation
    if src_layer.customLayerData:
        dst_layer.customLayerData = dict(src_layer.customLayerData)
    src_pr = src_layer.pseudoRoot
    dst_pr = dst_layer.pseudoRoot
    for key in ["upAxis", "metersPerUnit", "kilogramsPerUnit",
                "framesPerSecond", "timeCodesPerSecond",
                "startTimeCode", "endTimeCode"]:
        if key in src_pr.ListInfoKeys():
            try:
                dst_pr.SetInfo(key, src_pr.GetInfo(key))
            except Exception:
                pass


def _ensure_ancestors(src_stage, dst_layer, prim_path) -> None:
    src_layer   = src_stage.GetRootLayer()
    parent_path = prim_path.GetParentPath()
    if parent_path in (Sdf.Path.absoluteRootPath, Sdf.Path.emptyPath):
        return
    _ensure_ancestors(src_stage, dst_layer, parent_path)
    if dst_layer.GetPrimAtPath(parent_path):
        return
    src_spec = src_layer.GetPrimAtPath(parent_path)
    if not src_spec:
        return
    par_parent = parent_path.GetParentPath()
    if par_parent == Sdf.Path.absoluteRootPath:
        dst_spec = Sdf.PrimSpec(dst_layer, parent_path.name, src_spec.specifier)
    else:
        par_spec = dst_layer.GetPrimAtPath(par_parent)
        if not par_spec:
            return
        dst_spec = Sdf.PrimSpec(par_spec, parent_path.name, src_spec.specifier)
    dst_spec.typeName = src_spec.typeName
    for key in src_spec.ListInfoKeys():
        if key in ("specifier", "typeName"):
            continue
        try:
            dst_spec.SetInfo(key, src_spec.GetInfo(key))
        except Exception:
            pass
    for prop_spec in src_spec.properties.values():
        try:
            Sdf.CopySpec(src_layer, prop_spec.path, dst_layer, prop_spec.path)
        except Exception:
            pass


def _collect_sdf_internal_refs(src_layer, comp_path: Sdf.Path) -> set:
    targets: set = set()

    def _walk(spec):
        for lst in (spec.referenceList, spec.payloadList):
            for item in lst.GetAddedOrExplicitItems():
                if not item.assetPath and item.primPath and not item.primPath.isEmpty:
                    target = item.primPath
                    if not target.HasPrefix(comp_path):
                        targets.add(target)
        for child in spec.nameChildren.values():
            _walk(child)

    root_spec = src_layer.GetPrimAtPath(comp_path)
    if root_spec:
        _walk(root_spec)
    return targets


def _copy_external_prims(src_stage, src_layer, dst_layer, paths) -> None:
    for path in paths:
        if dst_layer.GetPrimAtPath(path):
            continue
        spec = src_layer.GetPrimAtPath(path)
        if not spec:
            continue
        _ensure_ancestors(src_stage, dst_layer, path)
        try:
            Sdf.CopySpec(src_layer, path, dst_layer, path)
        except Exception:
            pass


def _copy_usd_prototypes(src_layer, dst_layer) -> None:
    """USD 자동 인스턴싱 프로토타입(/__Prototype_N) 전체 복사."""
    for root_spec in src_layer.rootPrims:
        path = root_spec.path
        if str(path).startswith("/__Prototype_"):
            if not dst_layer.GetPrimAtPath(path):
                try:
                    Sdf.CopySpec(src_layer, path, dst_layer, path)
                except Exception:
                    pass


def _copy_prototype_scopes_by_name(src_layer, dst_layer, cfg) -> None:
    scope_names = cfg.get("prototype_scope_names", ["Prototypes"])
    if not scope_names or not src_layer.defaultPrim:
        return
    default_path = Sdf.Path("/" + src_layer.defaultPrim)
    for name in scope_names:
        child_path = default_path.AppendChild(name)
        if not src_layer.GetPrimAtPath(child_path):
            continue
        if dst_layer.GetPrimAtPath(child_path):
            continue
        if not dst_layer.GetPrimAtPath(default_path):
            dp_spec = src_layer.GetPrimAtPath(default_path)
            if dp_spec:
                Sdf.PrimSpec(dst_layer, default_path.name, dp_spec.specifier)
        parent_spec = dst_layer.GetPrimAtPath(default_path)
        if not parent_spec:
            continue
        try:
            Sdf.CopySpec(src_layer, child_path, dst_layer, child_path)
        except Exception:
            pass


def export_paths(src_stage, paths, output_path, cfg, log=print) -> str:
    """paths 목록을 output_path에 하나의 USD로 저장."""
    src_layer = src_stage.GetRootLayer()
    dst_layer = Sdf.Layer.CreateAnonymous()
    _copy_stage_metadata(src_layer, dst_layer)
    for path in paths:
        _ensure_ancestors(src_stage, dst_layer, path)
        try:
            Sdf.CopySpec(src_layer, path, dst_layer, path)
        except Exception:
            pass
    for root_spec in src_layer.rootPrims:
        if root_spec.path.name == src_layer.defaultPrim:
            continue
        if not dst_layer.GetPrimAtPath(root_spec.path):
            try:
                Sdf.CopySpec(src_layer, root_spec.path, dst_layer, root_spec.path)
            except Exception:
                pass
    ref_targets: set = set()
    for path in paths:
        ref_targets |= _collect_sdf_internal_refs(src_layer, path)
    _copy_external_prims(src_stage, src_layer, dst_layer, ref_targets)
    _copy_usd_prototypes(src_layer, dst_layer)
    _copy_prototype_scopes_by_name(src_layer, dst_layer, cfg)
    dst_layer.Export(output_path)
    dst_layer.Clear()
    del dst_layer
    gc.collect()
    return output_path


# ── Main process ───────────────────────────────────────────────────────────────

def process_stage(
    stage,
    usd_file_path: str,
    output_directory: str,
    cfg: dict,
    log=print,
) -> tuple[int, int, int, int]:
    """
    stage를 분류해서 output_directory에 저장.

    Returns:
        (util_count, floor_count, no_level_count, other_count)
    """
    merged = dict(DEFAULT_CFG)
    merged.update(cfg)
    cfg = merged

    src_basename  = os.path.splitext(os.path.basename(usd_file_path))[0]
    safe_basename = _sanitize_filename(src_basename)
    if cfg.get("subfolder_per_file", True):
        output_directory = os.path.join(output_directory, safe_basename)
    os.makedirs(output_directory, exist_ok=True)

    # debug_log 활성화 시 output_directory에 _fab_split_debug.log 저장
    _debug_file = None
    _debug_path = None
    if cfg.get("debug_log", False):
        try:
            _debug_path = os.path.join(output_directory, f"{safe_basename}_fab_split_debug.log")
            _debug_file = open(_debug_path, "w", encoding="utf-8")  # noqa: WPS515
            _orig_log = log
            def log(msg: str) -> None:  # type: ignore[misc]
                _orig_log(msg)
                try:
                    _debug_file.write(msg + "\n")
                    _debug_file.flush()
                except Exception:
                    pass
        except Exception as e:
            log(f"  [DEBUG_LOG] 파일 생성 실패: {e}")

    util_paths, floor_dict, no_level_paths, other_paths = collect_by_util_and_floor(stage, cfg, log=log)

    util_count     = 0
    floor_count    = 0
    no_level_count = 0
    other_count    = 0

    sep = cfg.get("suffix_sep", "@")

    # 배관류 → {파일명}@util.usd
    if util_paths:
        util_output = os.path.join(output_directory, f"{safe_basename}{sep}util{cfg['output_ext']}")
        export_paths(stage, util_paths, util_output, cfg, log=log)
        log(f"  [UTIL] {len(util_paths)} prims → {util_output}")
        util_count = 1

    # 층별 → {파일명}@{층이름}.usd
    for level_name, paths in sorted(floor_dict.items()):
        safe_level   = _sanitize_filename(level_name)
        floor_output = os.path.join(output_directory, f"{safe_basename}{sep}{safe_level}{cfg['output_ext']}")
        export_paths(stage, paths, floor_output, cfg, log=log)
        log(f"  [FLOOR:{level_name}] {len(paths)} prims → {floor_output}")
        floor_count += 1

    # 층 정보 없는 equipment → {파일명}@no_level.usd
    if no_level_paths:
        no_level_output = os.path.join(output_directory, f"{safe_basename}{sep}no_level{cfg['output_ext']}")
        export_paths(stage, no_level_paths, no_level_output, cfg, log=log)
        log(f"  [NO_LEVEL] {len(no_level_paths)} prims → {no_level_output}")
        no_level_count = 1

    # 나머지 (util도 equipment도 아닌 component) → {파일명}@other.usd
    if other_paths:
        other_output = os.path.join(output_directory, f"{safe_basename}{sep}other{cfg['output_ext']}")
        export_paths(stage, other_paths, other_output, cfg, log=log)
        log(f"  [OTHER] {len(other_paths)} prims → {other_output}")
        other_count = 1

    # 원본 USD → {파일명}@all.usd 로 복사
    all_output = os.path.join(output_directory, f"{safe_basename}{sep}all{cfg['output_ext']}")
    shutil.copy2(usd_file_path, all_output)
    log(f"  [ALL] 원본 복사 → {all_output}")

    if _debug_file and _debug_path:
        log(f"  [DEBUG_LOG] {_debug_path}")  # 닫기 전에 기록
        _debug_file.close()

    u, f, n, o = util_count, floor_count, no_level_count, other_count
    del util_paths, floor_dict, no_level_paths, other_paths
    gc.collect()
    return u, f, n, o
