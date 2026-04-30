# gaudiform-fab-splitter

USD FAB 파일을 EQP/UTIL/INFRA로 분리 — gaudiform 익스터널 스크립트 모듈

## 구조

```
gaudiform/
  core/
    fab_splitter/
      fab_splitter_core.py       — 핵심 로직 (pxr 단독 동작)
      fab_splitter_operation.py  — PostProcessOperation 구현 (gaudiform 스케줄러 연동)
      __init__.py
    __init__.py
  __init__.py
```

## 동작 방식

defaultPrim 하위 component prim을 순회하며 다음 기준으로 분류:

- **EQP**: `target_category`(Mechanical Equipment) + 지정 층 높이 범위 내
- **UTIL**: UTIL 카테고리(Pipes 등) 또는 높이 범위 밖의 Mechanical Equipment
- **INFRA**: EQP/UTIL에 포함되지 않은 나머지

분류된 컴포넌트를 SK_EQ_ID 단위로 개별 USD 파일로 저장합니다.

## 스케줄러 파라미터

```json
{
  "operation": "external",
  "script": "gaudiform/core/fab_splitter/fab_splitter_operation.py",
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
    "split_output_folders": true
  }
}
```

### 파라미터 상세

| 파라미터 | 기본값 | 설명 |
|---|---|---|
| `target_floor_name` | `"9th FL"` | 대상 층 이름 |
| `floor_z_auto` | `true` | `true`: 층 레벨 Z에서 범위 자동 계산 |
| `floor_z_min` | `0.0` | Z 범위 최솟값 (floor_z_auto=false 시 사용) |
| `floor_z_max` | `0.0` | Z 범위 최댓값 (floor_z_auto=false 시 사용) |
| `target_category` | `"Mechanical Equipment"` | EQP로 분류할 카테고리 |
| `util_categories` | `["Pipes", ...]` | UTIL로 분류할 카테고리 목록 |
| `output_prefix_eqp` | `"EQP_"` | EQP 출력 파일 prefix |
| `output_prefix_util` | `"UTIL_"` | UTIL 출력 파일 prefix |
| `output_prefix_infra` | `"INFRA_"` | INFRA 출력 파일 prefix |
| `output_ext` | `".usd"` | 출력 파일 확장자 |
| `split_output_folders` | `true` | `true`: EQP/UTIL/INFRA 폴더 분리 |
