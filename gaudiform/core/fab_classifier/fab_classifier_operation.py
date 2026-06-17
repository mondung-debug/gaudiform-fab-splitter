# -*- coding: utf-8 -*-
"""FabClassifier post-processing operation.

phase = "post_batch" — 배치 완료 후 출력 폴더의 USD 파일을 FAB별로 분류.

스케줄러 config.json 예시:
    {
      "post_processing": [
        {
          "operation": "external",
          "script": "gaudiform/core/fab_classifier/fab_classifier_operation.py",
          "params": {
            "input_dir":     "/data/converted",
            "output_dir":    "/data/classified",
            "fab_map": {
              "M15C": ["1st FL", "2nd FL", "3rd FL"],
              "M15D": ["5th FL", "6th FL", "7th FL"]
            },
            "copy_mode":     true,
            "unmatched_dir": "_unmatched"
          }
        }
      ]
    }

params:
    input_dir     (str)          — 분류할 USD 파일이 있는 폴더
    output_dir    (str)          — FAB별 서브폴더를 만들 출력 루트
    fab_map       (dict)         — {"FAB명": ["층 이름", ...]} 매핑
    copy_mode     (bool, true)   — true: 복사 / false: 이동
    unmatched_dir (str)          — 미매칭 파일 서브폴더명 (기본 "_unmatched")
"""

from __future__ import annotations

from gaudiform.core.post_processing import PostProcessOperation, PostProcessContext
from gaudiform.core.fab_classifier.fab_classifier_core import process_folder

_TAG = "FabClassifier"


class FabClassifierOperation(PostProcessOperation):
    phase = "post_batch"

    def execute(self, context: PostProcessContext) -> None:
        p = context.params

        input_dir     = p.get("input_dir")
        output_dir    = p.get("output_dir")
        fab_map       = p.get("fab_map", {})
        copy_mode     = bool(p.get("copy_mode", True))
        unmatched_dir = p.get("unmatched_dir", "_unmatched")

        if not input_dir:
            context.on_warn(_TAG, "'input_dir' param is required")
            return
        if not output_dir:
            context.on_warn(_TAG, "'output_dir' param is required")
            return
        if not fab_map:
            context.on_warn(_TAG, "'fab_map' param is required")
            return

        def _log(msg: str) -> None:
            if "[ERROR]" in msg or "[WARN]" in msg:
                context.on_warn(_TAG, msg.strip())
            else:
                context.on_info(_TAG, msg.strip())

        context.on_info(_TAG, f"FAB 분류 시작 — input: {input_dir}  output: {output_dir}")

        result = process_folder(
            input_dir=input_dir,
            output_dir=output_dir,
            fab_map=fab_map,
            copy_mode=copy_mode,
            unmatched_dir=unmatched_dir,
            log=_log,
        )

        for fab, files in result["matched"].items():
            context.on_info(_TAG, f"  {fab}: {len(files)}개")
        if result["unmatched"]:
            context.on_warn(_TAG, f"  미매칭: {len(result['unmatched'])}개 → {unmatched_dir}/")
