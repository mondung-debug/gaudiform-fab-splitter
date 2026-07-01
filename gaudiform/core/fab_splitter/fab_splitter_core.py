# -*- coding: utf-8 -*-
"""
FabSplitter core logic — USD stage를 배관류(_util)와 층별 파일로 분리.
pxr 단독으로 동작 (Kit/Omniverse 불필요).
"""

from __future__ import annotations

import gc
import os
import re

from pxr import Sdf, Usd, UsdGeom

# ── Metadata attribute names (Hoops Connector 규칙) ───────────────────────────

ATTR_CATEGORY   = "omni:hoops:metadata:Other:Category"
ATTR_LEVEL_NAME = "omni:hoops:metadata:tn__IdentityData_qC:Name"
ATTR_TYPE       = "omni:hoops:metadata:TYPE"

# ── Default config ─────────────────────────────────────────────────────────────

DEFAULT_CFG = {
    "util_categories":       ["Pipes", "Pipe Fittings", "Pipe Accessories", "Flex Pipes"],
    "output_ext":            ".usd",
    "prototype_scope_names": ["Prototypes"],
    "subfolder_per_file":    True,
    "normalize_level_name":  False,   # True 시 층 이름 정규화 (예: "9th FL" → "9F")
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


def _level_ancestor(prim):
    current = prim.GetParent()
    while current and current.GetPath() != Sdf.Path("/"):
        if _get_attr(current, ATTR_TYPE) == "IFCBUILDINGSTOREY":
            return current
        current = current.GetParent()
    return None


# ── Collection ─────────────────────────────────────────────────────────────────

def collect_by_util_and_floor(stage, cfg, log=print):
    """
    Returns:
        util_paths:    list[SdfPath] — util_categories에 속하는 컴포넌트
        floor_dict:    dict[str, list[SdfPath]] — 나머지, 층별 분류
        no_level_paths: list[SdfPath] — 층 정보 없는 나머지
    """
    util_cat_set   = set(cfg.get("util_categories", []))
    do_normalize   = cfg.get("normalize_level_name", False)
    util_paths: list = []
    floor_dict: dict = {}
    no_level_paths: list = []
    total = 0

    for prim in stage.TraverseAll():
        if Usd.ModelAPI(prim).GetKind() != "component":
            continue
        total += 1

        cat = _get_attr(prim, ATTR_CATEGORY)
        if cat in util_cat_set:
            util_paths.append(prim.GetPath())
        else:
            level_prim = _level_ancestor(prim)
            level_name = (_get_attr(level_prim, ATTR_LEVEL_NAME) or "") if level_prim else ""
            if level_name:
                if do_normalize:
                    level_name = _normalize_level_name(level_name)
                floor_dict.setdefault(level_name, []).append(prim.GetPath())
            else:
                no_level_paths.append(prim.GetPath())

    log(f"  Collection: total={total}, util={len(util_paths)}, "
        f"floors={sorted(floor_dict.keys())}, no_level={len(no_level_paths)}")
    return util_paths, floor_dict, no_level_paths


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
) -> tuple[int, int, int]:
    """
    stage를 배관류(_util)와 층별 파일로 분리해서 output_directory에 저장.

    Returns:
        (util_count, floor_count, no_level_count)
    """
    merged = dict(DEFAULT_CFG)
    merged.update(cfg)
    cfg = merged

    src_basename  = os.path.splitext(os.path.basename(usd_file_path))[0]
    safe_basename = _sanitize_filename(src_basename)
    if cfg.get("subfolder_per_file", True):
        output_directory = os.path.join(output_directory, safe_basename)
    os.makedirs(output_directory, exist_ok=True)

    # 인스턴스(prototype)가 있으면 flatten해서 프록시 없이 처리
    if stage.GetPrototypes():
        log("  [INFO] Prototype 감지 → stage flatten 처리 중...")
        flat_layer = stage.Flatten()
        work_stage = Usd.Stage.Open(flat_layer)
        log("  [INFO] Flatten 완료")
    else:
        work_stage = stage

    util_paths, floor_dict, no_level_paths = collect_by_util_and_floor(work_stage, cfg, log=log)

    util_count     = 0
    floor_count    = 0
    no_level_count = 0

    # 배관류 → {파일명}_util.usd
    if util_paths:
        util_output = os.path.join(output_directory, f"{safe_basename}_util{cfg['output_ext']}")
        export_paths(work_stage, util_paths, util_output, cfg, log=log)
        log(f"  [UTIL] {len(util_paths)} prims → {util_output}")
        util_count = 1

    # 층별 → {파일명}_{층이름}.usd
    for level_name, paths in sorted(floor_dict.items()):
        safe_level   = _sanitize_filename(level_name)
        floor_output = os.path.join(output_directory, f"{safe_basename}_{safe_level}{cfg['output_ext']}")
        export_paths(work_stage, paths, floor_output, cfg, log=log)
        log(f"  [FLOOR:{level_name}] {len(paths)} prims → {floor_output}")
        floor_count += 1

    # 층 정보 없는 나머지 → {파일명}_no_level.usd
    if no_level_paths:
        no_level_output = os.path.join(output_directory, f"{safe_basename}_no_level{cfg['output_ext']}")
        export_paths(work_stage, no_level_paths, no_level_output, cfg, log=log)
        log(f"  [NO_LEVEL] {len(no_level_paths)} prims → {no_level_output}")
        no_level_count = 1

    # flatten한 경우 임시 스테이지 정리
    if work_stage is not stage:
        del work_stage
        gc.collect()

    return util_count, floor_count, no_level_count
