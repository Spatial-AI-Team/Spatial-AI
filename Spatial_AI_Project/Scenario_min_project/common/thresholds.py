# -*- coding: utf-8 -*-
"""임계 상수 유일 허용 파일(pre-commit no-literal-thresholds 훅 대상).

threshold_set_id로 완전히 외부화하는 건 아직 미정(CLAUDE.md "설정으로 열어둘 것" 표,
`decisions/DV_DEFAULTS.md`) — 이 모듈은 우선 "같은 이름의 상수가 여러 파일에 흩어져
값이 갈리는 사고"(LEAD_IN 4중복·OBST_CUTIN_Y vs CUTIN_Y 값 충돌 실측)를 막는 최소
요건만 충족한다. 여기 없는 임계값이 새 파일에 등장하면 여기로 옮기고 import한다.

주의(2026-09-08): `task_episode/vlm_verify.py`는 여전히 `LEAD_IN = 3.0`을 로컬로 갖고
있다(이번 커밋 범위 밖 — 건드리지 않음). `task_episode/tag_v08.py`는 2026-09-21에 이관
완료(pre-commit no-literal-thresholds 훅이 처음으로 실제 커밋을 막아 이관).

2026-09-21 추가 이관: `common/config.py`의 `NUM_FRAMES`·`EVENT_DECEL_AX`·`OBST_CUTIN_Y` —
같은 훅이 실제 커밋 시점에 처음 걸려 이관. `common/dataset.py`·`common/events.py`의
import를 `thresholds`로 갈아탐(값 변경 없음).
"""

LEAD_IN = 3.0        # 에피소드 onset 이전 접근 구간(초)
VALID_FRAC = 0.5     # map_lane 유효 프레임 비율 게이트
NUM_FRAMES = 16       # scene understanding 용 시간축 균등 샘플 프레임 수
EVENT_DECEL_AX = -1.0 # ax(m/s^2) 감속 임계
OBST_CUTIN_Y = 2.8    # cut-in 판정: 차로 밖(|y|>이값)에서 진입

# 물리 게이트(준거 B, verification/gate.py) — proximity_range(reachable에 통합, verifier.md).
# tag_v08.py의 in-path 필터(role in crossing/preceding_vehicle, min_dist<30)와 다른 값이다:
# 그쪽은 "critical_component 후보로 볼지"의 S1 판단 임계, 이건 "이미 원인으로 확정된 값이
# 물리적으로 말이 되는지"의 검증측 임계 — 같은 30 값을 그대로 재사용하지 않고 검증 측 여유를
# 둔다(생산 임계를 검증이 그대로 베끼면 게이트가 항상 통과해 무의미해짐).
GATE_PROXIMITY_MAX_M = 45.0
