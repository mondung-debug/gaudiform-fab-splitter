# -*- coding: utf-8 -*-
"""
FabSplitter core logic — USD stage를 EQP/UTIL/INFRA로 분리.
pxr 단독으로 동작 (Kit/Omniverse 불필요).
"""

from __future__ import annotations

import os
import re

from pxr import Sdf, Usd, UsdGeom

# ── Metadata attribute names (Hoops Connector 규칙) ───────────────────────────

ATTR_CATEGORY   = "omni:hoops:metadata:Other:Category"
ATTR_SK_EQ_ID   = "omni:hoops:metadata:tn__IdentityData_qC:SK_EQ_ID"
ATTR_LEVEL_NAME = "omni:hoops:metadata:tn__IdentityData_qC:Name"
ATTR_TYPE       = "omni:hoops:metadata:TYPE"

# ── Default config ─────────────────────────────────────────────────────────────

DEFAULT_CFG = {
    "target_floor_name":      "9th FL",
    "floor_z_min":            0.0,
    "floor_z_max":            0.0,
    "floor_z_auto":           True,
    "target_category":        "Mechanical Equipment",
    "util_categories":        ["Pipes", "Pipe Fittings", "Pipe Accessories", "Flex Pipes"],
    "output_prefix_eqp":      "EQP_",
    "output_prefix_util":     "UTIL_",
    "output_prefix_infra":    "INFRA_",
    "output_ext":             ".usd",
    "split_output_folders":   True,
    "normalize_sk_eq_id":     True,   # _숫자 suffix 자동 제거 (abc123_1 → abc123)
    "log_sk_eq_id_fix":       True,   # 수정된 SK_EQ_ID 로그 출력
}


# ── Helpers ────────────────────────────────────────────────────────────────────

_SK_EQ_ID_SUFFIX_RE = re.compile(r'_\d+$')

def _normalize_sk_eq_id(raw_id: str) -> str:
    """SK_EQ_ID 끝의 _숫자 suffix 제거. ex) abc123_1 → abc123"""
    return _SK_EQ_ID_SUFFIX_RE.sub('', raw_id)


def _get_attr(prim, attr_name):
    attr = prim.GetAttribute(attr_name)
    if attr and attr.HasValue():
        return attr.Get()
    return None


def find_floor_levels(stage) -> dict[str, float]:
    """IFCBUILDINGSTOREY xformOp world Z 수집 → {name: z}"""
    levels: dict[str, float] = {}
    for prim in stage.TraverseAll():
        if _get_attr(prim, ATTR_TYPE) != "IFCBUILDINGSTOREY":
            continue
        name = _get_attr(prim, ATTR_LEVEL_NAME)
        if name and name not in levels:
            xf  = UsdGeom.Xformable(prim)
            mat = xf.ComputeLocalToWorldTransform(Usd.TimeCode.Default())
            levels[name] = mat.ExtractTranslation()[2]
    return levels


def _is_bbox_in_range(prim, bbox_cache, z_min, z_max) -> bool:
    try:
        bbox  = bbox_cache.ComputeWorldBound(prim)
        rng   = bbox.ComputeAlignedRange()
        if rng.IsEmpty():
            return False
        return rng.GetMin()[2] <= z_max and rng.GetMax()[2] >= z_min
    except Exception:
        return False


def _level_ancestor(prim):
    current = prim.GetParent()
    while current and current.GetPath() != Sdf.Path("/"):
        if _get_attr(current, ATTR_TYPE) == "IFCBUILDINGSTOREY":
            return current
        current = current.GetParent()
    return None


# ── Collection ─────────────────────────────────────────────────────────────────

def collect_components(
    stage,
    bbox_cache,
    cfg: dict,
    log=print,
) -> tuple[dict, dict]:
    """EQP / UTIL dict 반환. {sk_eq_id: [SdfPath, ...]}"""
    z_min        = cfg["floor_z_min"]
    z_max        = cfg["floor_z_max"]
    target_floor = cfg["target_floor_name"]
    target_cat   = cfg["target_category"]
    util_cat_set = set(cfg.get("util_categories", []))

    do_normalize  = cfg.get("normalize_sk_eq_id", True)
    do_log_fix    = cfg.get("log_sk_eq_id_fix", True)

    eqp_dict: dict  = {}
    util_dict: dict = {}
    stats = {"total": 0, "eqp": 0, "util_cat": 0,
             "util_height": 0, "infra_no_id": 0, "infra_other": 0, "id_fixed": 0}

    for prim in stage.TraverseAll():
        if Usd.ModelAPI(prim).GetKind() != "component":
            continue
        stats["total"] += 1

        sk_eq_id = _get_attr(prim, ATTR_SK_EQ_ID)
        if not sk_eq_id:
            stats["infra_no_id"] += 1
            continue

        sk_eq_id = str(sk_eq_id)
        if do_normalize:
            normalized = _normalize_sk_eq_id(sk_eq_id)
            if normalized != sk_eq_id:
                stats["id_fixed"] += 1
                if do_log_fix:
                    log(f"  [SK_EQ_ID FIX] {prim.GetPath().name}: '{sk_eq_id}' -> '{normalized}'")
                sk_eq_id = normalized

        cat        = _get_attr(prim, ATTR_CATEGORY)
        level_prim = _level_ancestor(prim)
        level_name = (_get_attr(level_prim, ATTR_LEVEL_NAME) or "") if level_prim else ""

        if cat == target_cat:
            if _is_bbox_in_range(prim, bbox_cache, z_min, z_max):
                stats["eqp"] += 1
                if level_name != target_floor:
                    log(f"  [EQP HEIGHT-MATCH] {prim.GetPath().name} (SK={sk_eq_id})"
                        f" on '{level_name}' but within {target_floor} height range")
                eqp_dict.setdefault(sk_eq_id, []).append(prim.GetPath())
            else:
                stats["util_height"] += 1
                util_dict.setdefault(sk_eq_id, []).append(prim.GetPath())
        elif cat in util_cat_set:
            stats["util_cat"] += 1
            util_dict.setdefault(sk_eq_id, []).append(prim.GetPath())
        else:
            stats["infra_other"] += 1

    log(f"  Collection: total={stats['total']}, "
        f"EQP={stats['eqp']} ({len(eqp_dict)} IDs), "
        f"UTIL_cat={stats['util_cat']}, UTIL_height={stats['util_height']}, "
        f"INFRA_no_id={stats['infra_no_id']}, INFRA_other={stats['infra_other']}, "
        f"ID_fixed={stats['id_fixed']}")
    return eqp_dict, util_dict


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


def _all_prim_specs(layer):
    result = []
    def _walk(spec):
        for child in list(spec.nameChildren):
            result.append(child)
            _walk(child)
    for root in layer.rootPrims:
        result.append(root)
        _walk(root)
    return result


def export_group(src_stage, sk_eq_id, component_paths, output_dir, prefix, cfg) -> str:
    src_layer   = src_stage.GetRootLayer()
    output_path = os.path.join(output_dir, f"{prefix}{sk_eq_id}{cfg['output_ext']}")
    dst_layer   = Sdf.Layer.CreateAnonymous()
    _copy_stage_metadata(src_layer, dst_layer)
    for comp_path in component_paths:
        _ensure_ancestors(src_stage, dst_layer, comp_path)
        Sdf.CopySpec(src_layer, comp_path, dst_layer, comp_path)
    for root_spec in src_layer.rootPrims:
        if root_spec.path.name == src_layer.defaultPrim:
            continue
        if not dst_layer.GetPrimAtPath(root_spec.path):
            try:
                Sdf.CopySpec(src_layer, root_spec.path, dst_layer, root_spec.path)
            except Exception:
                pass
    dst_layer.Export(output_path)
    return output_path


def export_infra(src_stage, excluded_paths, output_dir, filename, cfg) -> tuple[str, int]:
    src_layer   = src_stage.GetRootLayer()
    output_path = os.path.join(
        output_dir, f"{cfg['output_prefix_infra']}{filename}{cfg['output_ext']}")
    dst_layer   = Sdf.Layer.CreateAnonymous()
    _copy_stage_metadata(src_layer, dst_layer)
    for root_spec in src_layer.rootPrims:
        try:
            Sdf.CopySpec(src_layer, root_spec.path, dst_layer, root_spec.path)
        except Exception:
            pass
    removed = 0
    for path in excluded_paths:
        if dst_layer.GetPrimAtPath(path):
            parent_spec = dst_layer.GetPrimAtPath(path.GetParentPath())
            if parent_spec:
                try:
                    del parent_spec.nameChildren[path.name]
                    removed += 1
                except Exception:
                    pass
    changed = True
    while changed:
        changed = False
        for spec in _all_prim_specs(dst_layer):
            if spec.typeName == "Xform" and len(list(spec.nameChildren)) == 0:
                parent = dst_layer.GetPrimAtPath(spec.path.GetParentPath())
                if parent and spec.path != Sdf.Path("/"):
                    try:
                        del parent.nameChildren[spec.path.name]
                        changed = True
                        break
                    except Exception:
                        pass
    dst_layer.Export(output_path)
    return output_path, removed


# ── Main process ───────────────────────────────────────────────────────────────

def _get_output_dir(base_dir, category, src_basename, cfg) -> str:
    if cfg.get("split_output_folders", True):
        d = os.path.join(base_dir, category, src_basename)
    else:
        d = os.path.join(base_dir, src_basename)
    os.makedirs(d, exist_ok=True)
    return d


def process_stage(
    stage,
    usd_file_path: str,
    output_directory: str,
    cfg: dict,
    log=print,
) -> tuple[int, int, int]:
    """
    stage를 EQP/UTIL/INFRA로 분리해서 output_directory에 저장.

    Returns:
        (eqp_count, util_count, infra_count)
    """
    merged = dict(DEFAULT_CFG)
    merged.update(cfg)
    cfg = merged

    src_basename = os.path.splitext(os.path.basename(usd_file_path))[0]
    bbox_cache   = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_])

    # 층 Z 범위 결정
    levels = find_floor_levels(stage)
    for name, z in sorted(levels.items(), key=lambda x: x[1]):
        marker = " ← TARGET" if name == cfg["target_floor_name"] else ""
        log(f"  [FLOOR] {name}: Z={z:.3f}m{marker}")

    if cfg.get("floor_z_auto"):
        target_z = levels.get(cfg["target_floor_name"])
        if target_z is None:
            log(f"  [WARN] '{cfg['target_floor_name']}' not found in stage")
            return 0, 0, 0
        sorted_z = sorted(levels.values())
        idx      = sorted_z.index(target_z)
        z_min    = target_z
        z_max    = sorted_z[idx + 1] if idx + 1 < len(sorted_z) else target_z + 10.0
        cfg = dict(cfg)
        cfg["floor_z_min"] = z_min
        cfg["floor_z_max"] = z_max
        log(f"  [AUTO] '{cfg['target_floor_name']}' Z={target_z:.3f} → range: [{z_min:.3f}, {z_max:.3f}]")
    else:
        log(f"  [CONFIG] height range: [{cfg['floor_z_min']:.1f}, {cfg['floor_z_max']:.1f}]")

    eqp_dict, util_dict = collect_components(stage, bbox_cache, cfg, log=log)

    # EQP 저장
    eqp_out_dir = _get_output_dir(output_directory, "EQP", src_basename, cfg)
    for sk_eq_id, paths in sorted(eqp_dict.items()):
        out = export_group(stage, sk_eq_id, paths, eqp_out_dir,
                           cfg["output_prefix_eqp"], cfg)
        log(f"  [EQP] {sk_eq_id} ({len(paths)} prims) → {out}")

    # UTIL 저장
    util_out_dir = _get_output_dir(output_directory, "UTIL", src_basename, cfg)
    for sk_eq_id, paths in sorted(util_dict.items()):
        out = export_group(stage, sk_eq_id, paths, util_out_dir,
                           cfg["output_prefix_util"], cfg)
        log(f"  [UTIL] {sk_eq_id} ({len(paths)} prims) → {out}")

    # INFRA 저장
    infra_out_dir = _get_output_dir(output_directory, "INFRA", src_basename, cfg)
    all_paths     = [p for ps in eqp_dict.values() for p in ps] + \
                    [p for ps in util_dict.values() for p in ps]
    infra_count = 0
    if all_paths:
        infra_path, removed = export_infra(stage, all_paths, infra_out_dir,
                                           src_basename, cfg)
        log(f"  [INFRA] removed {removed} prims → {infra_path}")
        infra_count = 1

    return len(eqp_dict), len(util_dict), infra_count
