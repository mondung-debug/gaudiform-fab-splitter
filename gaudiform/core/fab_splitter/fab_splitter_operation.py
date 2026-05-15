# -*- coding: utf-8 -*-
"""FabSplitter post-processing operation.

phase = "per_file" — 변환된 USD 파일을 EQP/UTIL/INFRA로 분리하고
output_directory 하위에 저장합니다.

스케줄러 config.json 예시:
    {
      "post_processing": [
        {
          "operation": "external",
          "script": "external_operations/fab_splitter_operation.py",
          "params": {
            "target_floor_name":    "9th FL",
            "floor_z_auto":         true,
            "floor_z_min":          0.0,
            "floor_z_max":          0.0,
            "target_category":      "Mechanical Equipment",
            "util_categories":      ["Pipes", "Pipe Fittings", "Pipe Accessories", "Flex Pipes"],
            "output_prefix_eqp":    "EQP_",
            "output_prefix_util":   "UTIL_",
            "output_prefix_infra":  "INFRA_",
            "output_ext":           ".usd",
            "split_output_folders": true,
            "normalize_sk_eq_id":   true,
            "log_sk_eq_id_fix":     true
          }
        }
      ]
    }
"""

from __future__ import annotations

from gaudiform.core.post_processing import PostProcessOperation, PostProcessContext
from gaudiform.core.fab_splitter.fab_splitter_core import process_stage

_TAG = "FabSplitterOperation"


class FabSplitterOperation(PostProcessOperation):
    """USD stage를 EQP/UTIL/INFRA로 분리하는 오퍼레이션."""

    phase = "per_file"

    def execute(self, context: PostProcessContext) -> None:
        stage = context.stage
        if stage is None:
            context.on_warn(_TAG, "stage가 없습니다. 스킵합니다.")
            return

        usd_file_path    = context.usd_file_path
        output_directory = context.output_directory or ""
        if not output_directory:
            context.on_warn(_TAG, "output_directory가 없습니다. 스킵합니다.")
            return

        cfg = context.params

        def _log(msg: str) -> None:
            if "[WARN]" in msg:
                context.on_warn(_TAG, msg.strip())
            else:
                context.on_info(_TAG, msg.strip())

        context.on_info(_TAG, f"FAB 분리 시작: {usd_file_path}")

        eqp_count, util_count, infra_count = process_stage(
            stage=stage,
            usd_file_path=usd_file_path,
            output_directory=output_directory,
            cfg=cfg,
            log=_log,
        )

        context.on_info(
            _TAG,
            f"완료: EQP {eqp_count}개 + UTIL {util_count}개 + INFRA {infra_count}개 파일 생성"
        )
