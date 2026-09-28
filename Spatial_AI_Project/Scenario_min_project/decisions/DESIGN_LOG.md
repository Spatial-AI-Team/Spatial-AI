# 설계 변경/개선 이력 (최신이 위)

> 각 엔트리는 소속 **Task**를 명시한다(컨벤션: `../docs/README.md`). Task = common | selection | episode(Track1|Track2) | multi.

---

## [2026-09-21] 고속 차선변경 미검출 — common/events.py 요레이트 게이트·측방변위 적분창 결함 (확인·기록만, 미수정)
> **Task**: multi

**배경 — 사용자 제기(물리 계산)**: 사용자가 Stage1(`task_selection`)의 lane_change 검출에
대해 차선변경의 물리량을 직접 계산해 의문을 제기했다.

| 케이스 | 조건 | 최대 heading 편위 | 피크 요레이트 |
|---|---|---|---|
| 도심 | 10 m/s, 3.5 m를 3초에 | 약 13° | 약 0.15 rad/s |
| 고속 | 25 m/s, 3.5 m를 4초에 | 약 4° | 약 0.035 rad/s |

제기된 우려 3가지: ① 고속 차선변경은 후보 구간 자체가 안 만들어진다 ② 도심도 검출이
불안정하다 ③ 측방변위 적분창이 좁아 하한(1.8 m)을 못 채운다.

이번 세션에서 코드를 직접 대조한 결과 **세 우려 모두 확정**됐다.

### 발견 1 — Track1/Track2/Stage1이 전부 같은 검출 코드를 공유한다
`common/events.py`의 `detect_events()`를 Stage1(`task_selection/selection.py:35`,
`task_selection/folder_selection.py:47,139`)과 Stage2(Track1 `task_episode/tag_v08.py:236`,
Track2 `task_episode/candidates.py`, 러너 `task_episode/run_pipeline.py:86`)가 **동일하게
호출**한다. 호출부 차이는 `curvature_fn` 인자뿐이고(Stage1은 `None`=map 미사용, Stage2는
map 기반 잔차보정 클로저), **yaw_rate 후보 게이트와 측방변위 적분창 로직은 완전히 동일**
하다. 이 사실은 `common/events.py:139-141` 주석("미지정(기본값, Stage1/task_selection이
쓰는 경로)이면 raw heading 그대로 … Stage2/task_episode 호출부만 `map_lane.
road_curvature_over`를 감싼 클로저를 넘겨 잔차 보정을 켠다")과
`docs/design/pipeline_design_guide_v0.4.md:180`("`common/events.py::detect_events()`는
Stage1·Stage2가 공유하는 map-free 함수")에 이미 명시돼 있다.

→ **이 결함은 Track1 또는 Track2 개별 문제가 아니라 공용 앵커 검출 인프라의 결함이다.**
곡률보정(`curvature_fn`)은 후보 구간이 만들어진 뒤의 heading **잔차 분류**에만 개입하므로
이 결함을 완화하지 않는다.

### 발견 2 — 실제 임계값 (`common/config.py:49-56`)
| 상수 | 값 | 역할 |
|---|---|---|
| `EVENT_TURN_YAWRATE` | `0.15` | 후보 구간 게이트 — `abs(yaw_rate) >` 이 값이 연속 유지돼야 후보 생성 |
| `EVENT_MIN_SEC` | `0.4` | 이벤트 최소 지속(초) |
| `EVENT_LC_HEADING_MAX` | `22.0` | 차선변경 net heading 상한(deg) |
| `EVENT_LC_LAT_MIN` | `1.8` | 차선변경 측방변위 하한(m) |
| `EVENT_LC_LAT_MAX` | `6.0` | 차선변경 측방변위 상한(m) |

판정 코드 `common/events.py:170-193` 요지:
```python
for i0, i1, a, b in _runs(np.abs(yr) > EVENT_TURN_YAWRATE, t, EVENT_MIN_SEC):   # 170
    ...
    lat = trapezoid(speed[i0:i1+1] * sin(yaw[i0:i1+1] - yaw[i0]), t[i0:i1+1])   # 175-177
    ...
    elif amag <= EVENT_LC_HEADING_MAX and EVENT_LC_LAT_MIN <= abs(lat) <= EVENT_LC_LAT_MAX:
        kind = "lane_change_left" if lat > 0 else "lane_change_right"           # 190-191
    else:
        continue                                                                # 193
```

### 발견 3 — 세 우려의 확정 근거 (성격이 서로 다르므로 구분한다)

**3-1. 고속 차선변경 = 확정된 미검출 (구조적 불가, 확률 문제 아님)**
`_runs()`(`common/events.py:104-118`)는 `abs(yaw_rate) > 0.15`가 연속 `EVENT_MIN_SEC=0.4`초
이상 유지돼야 후보 구간을 만든다. 고속 피크 요레이트 0.035 rad/s는 임계값의 **1/4 수준**
이라 mask가 **단 한 프레임도 True가 되지 않는다** → `_runs` 반환값이 빈 리스트 → 170행
루프 본문이 한 번도 실행되지 않음 → lane_change/turn 분기(188-193행) **진입 자체가 불가능**
하다. 측방변위·heading 판정이 아무리 옳아도 그 코드에 도달하지 않는다.

**3-2. 도심 차선변경 = 경계 불안정 확정 (임계값 설계상 필연)**
도심 피크 요레이트(약 0.15)가 게이트 임계값(`0.15`)과 **정확히 같다**. 조건이 `>`(초과)
이므로 피크가 임계값에 닿기만 하는 프로파일은 mask가 True가 되지 않고, 넘더라도 피크
근방의 짧은 구간만 True가 돼 그 지속시간이 `EVENT_MIN_SEC=0.4`초를 못 채울 가능성이 높다.
**검출 여부가 노이즈·샘플링에 좌우되는 경계 조건**이라는 뜻이며, 이는 실측 편차가 아니라
임계값 설계에서 필연적으로 따라오는 불안정도다.

**3-3. 측방변위 절단 확정 (게이트를 통과해도 값이 과소산출)**
`lat` 적분(175-177행)이 `_runs`가 반환한 **같은 `i0:i1+1` 구간** — 즉 요레이트가 임계값을
넘는 좁은 구간 — 에서만 이뤄진다. 차선변경의 진입·이탈 완만한 구간(요레이트는 낮지만
실제 측방 이동은 계속되는 구간)은 **적분 범위 밖이라 통째로 빠진다**. 실제 물리 측방변위가
3.5 m여도 좁은 창에서 적분한 `lat`이 하한 `EVENT_LC_LAT_MIN=1.8` m에 못 미치면 190행 조건이
거짓이 되고 193행 `continue`로 **조용히 버려진다**. 3-1·3-2를 통과한 도심 케이스조차 여기서
다시 탈락할 수 있다 — 세 결함은 직렬로 누적된다.

**로그 부재로 관측이 막혀 있다**: 193행 `continue`는 탈락 사실을 아무 데도 남기지 않는다.
위 `[2026-09-08] S0 단계 검증 clip 50개 추출` 항목의 "남겨둔 한계"(`decisions/
DESIGN_LOG.md:682-683`, 이 항목 삽입 전 기준)가 이미 "지침서 §2.3.1이 요구하는 '필터 통과
직전 탈락 경계 사례' 로그는 `detect_events()`가 중간 후보를 노출하지 않아 커버 불가 —
필요해지면 `events.py` 자체 수정 필요"로 기록해둔 것과 **같은 지점**이다. 그래서 이 결함은
지금까지 산출물 통계로 드러나지 않았다 — 미검출이 "이벤트 없음"과 구별되지 않는다.

### 판정 — 이번 사이클에서 수정하지 않는다
사용자 지시는 **"확인, 기록까지만"**이다. 따라서 이번 사이클에서 **코드·임계값을 변경하지
않는다**. `common/events.py`·`common/config.py`·`common/thresholds.py` 무변경, 파이프라인
재실행 없음. 이 항목은 결정이 아니라 **확정된 사실의 등재**이며, 되돌린 결정이 없으므로
`vocab_lint.py`의 `RETIRED` 등록 요청도 **없다**(폐기된 표현이 발생하지 않았다).

### 기존 결정과의 충돌 (판정하지 않고 드러내기만 한다)
`CLAUDE.md` §2 "전이 검출 4중 필터" 1번은 **"횡방향은 누적 방위 변화량(순간 요레이트
아님)"**으로 크기 필터를 규정한다. 그러나 현행 `common/events.py:170`의 후보 게이트는
**순간 요레이트**(`np.abs(yr) > 0.15`)다. 즉 현행 구현은 목표 설계 §2의 필터 1과 어긋나
있고, 발견 3-1의 미검출은 바로 이 어긋남의 직접적 귀결이다. 목표 설계 쪽이 이 결함을 이미
구조적으로 배제하고 있다는 점에서 **§2와 충돌하는 것은 현행 코드**이며 §2 변경 필요는 없다.
**판정 주체**: 사람 또는 rule-engineer(전이·앵커 담당).

또한 §3 불변 규칙의 "전이 검출 = CAN 규칙 확정"은 **유효하게 유지된다** — 이 결함은 검출을
규칙에서 모델로 옮기라는 근거가 아니라 **CAN 규칙 자체의 커버리지 구멍**이다. 앵커 비오염
원칙(설계 원칙 1)은 그대로 적용되며, 재설계도 규칙 영역 안에서 이뤄져야 한다.

### 미결 등재 (신규)
**고속 차선변경 검출 방법 재설계 필요.** 검토 후보:
- 요레이트 게이트 대신 **GPS/pose 경로 기반 측방변위 직접 계산** 방식
- 게이트 임계값을 **속도 적응형**으로 변경(요레이트 임계를 속도의 함수로)
- 적분창을 요레이트 게이트 구간이 아니라 **거동 완료·안정화 시점까지** 확장(§2 "시간창은
  거동 완료·안정화 시점까지 포함" 규정과 정합)
- 선행 요건: 193행 탈락 사례 로그 확보 — 그 전에는 변경 전후 비교의 분모가 없다

**결정 주체**: 사람(착수 승인) → rule-engineer(설계·구현). **선행 조건**: ① 사용자의 착수
판정 ② 경계 탈락 로그로 현행 미검출률 실측 ③ 변경은 Stage1·Stage2 **양쪽에 동시 파급**되므로
§4 전환 원칙("1 브랜치 = 1 변경 축")에 따라 단독 변경축으로 분리. **착수 여부는 사람 판정
대기.**

### 영향
문서만 수정: `decisions/DESIGN_LOG.md`(이 항목 추가). 코드·스키마·임계값 변경 없음.
파이프라인 재실행 없음. 산출물 무변경. 커밋하지 않음(워킹트리 유지).

---

## [2026-09-14] `experiment_design_v0.1.md` 정본 확정 — main 최종 저장본(`실험 설계 정본 v0.1`) 채택, worktree 5-arm 초안은 폐기본
> **Task**: multi

**배경**: 바로 아래 `[2026-09-11] 실험계획_추론경로_260824.md 실체 확인` 항목이 "worktree loose
파일을 내용 무변경으로 복사"라고 기록했으나 사실과 다르다. 같은 경로 `docs/experiments/
experiment_design_v0.1.md`를 두고 내용이 다른 두 판본이 있었다 — worktree 원본(제목 `실험 계획 —
추론 경로 및 모델 대조군 (2026-08-24)`, 5-arm)과 main 현재판(제목 `실험 설계 정본 v0.1`, A1~A4
4-arm, 18:38 저장). 이번 세션 조사에서 main 현재판은 에이전트가 임의로 개정한 것이 아니라
**사용자가 IDE에서 직접 편집·저장한 최종본**임이 확인됐다.

**결정(사용자, 2026-09-14)**: "가장 최근에 저장했던 파일로 진행" — main 현재판
(`실험 설계 정본 v0.1`)을 **정본으로 확정**한다. worktree의 2026-08-24 초안은 폐기본이며
되살리지 않는다(파일 자체는 삭제·이동 없이 그대로 둔다).

**되돌린 결정** (worktree 초안 → 정본에서 뒤집힘)

| 축 | worktree 초안(2026-08-24, 폐기) | 정본 `실험 설계 정본 v0.1`(채택) |
|---|---|---|
| arm 개수 | ①~⑤ 5개 | A1~A4 4개 |
| 기준선 | ① 현행 제약 디코딩 = arm 기준선 | **기준선 자격 부정** — CoT 미통제라 재현되지 않음. arm 비교에서 제외하고 보고용 1회 측정만(§A.5) |
| 재프리필 | ③ 생성+재프리필을 **arm으로** 취급 | **arm 아님** — A3의 후처리 옵션으로 강등(§A.4). 라벨은 안 바뀌고 확신도·엔트로피만 얻음 |
| 실험 C | "선행 판정 필요 — §2.2 상충, A1/A2/A3 중 선택 후 확정"(미결) | **상충 판정 완료** — GT 3-way 확정, all-after는 **반증 arm**. GT 전달 형식(좌표 텍스트/영상 오버레이/채널 분리) 부속 확인 추가 |
| 실험 D 대조군 | `Qwen3-VL` 명시 | 모델명 제거 — "동일 아키텍처 계열 base 모델" |
| 구현 요구 | 없음 | §A.7 신설 — 필수 스위치 5개(`inference_path`·`scoring_method`·`cot_mode`·`conditioning`·`reprefill_enabled`) + 점수화 주의 5항 + 재현성 요구 |

- 정본의 실험 C 판정은 `D-2026-08-24-05`(GT 3-way 확정)와 정합한다 — 충돌 없음.
- 기각된 표현: **`현행 제약 디코딩 = 기준선 arm` 기각**(기준선 자격 없음 — 보고용 측정으로만 잔존) /
  **`재프리필 arm` 기각**(A3 후처리 옵션으로 잔존). 두 표현 모두 문서·프롬프트에서 다시 쓰지 않는다.

**RETIRED 등록 요청**: 위 두 표현을 `vocab_lint.py`의 `RETIRED`에 등록하도록 schema-keeper에 요청
(`"현행 제약 디코딩 = 기준선 arm"`, `"재프리필 arm"`). design-scribe는 코드·스키마를 직접 고치지
않으므로 **등록 완료 여부는 schema-keeper 회신으로 확인**한다.

**미결 갱신**: 직전 계획에서 열어둔 "worktree 판을 폐기본으로 확정할지, 두 판을 별도 파일로
병존시킬지"는 **해소됨** — main 현재판(정본 v0.1)으로 확정, 사용자 결정(2026-09-14).

**충돌 확인(신규, 판정 요청)**: 정본 §A.7 스위치 표의 `conditioning` 기본값은 **`weak`**인데,
`CLAUDE.md` §2 "설정으로 열어둘 것" 표의 조건화 강도 기본값은 **`strong`**이다. 같은 스위치의
기본값이 두 정본 문서에서 어긋난다. 이 항목이 없으면 실험 A의 A2↔A4 대조가 어느 쪽을 기준
조건으로 잡았는지 재현할 수 없다. **판정 주체**: 사람 또는 experiment-runner. **선행 조건**:
실험 A의 A2↔A4 결과(조건화 강도의 일관성 이득 vs 오류 전파). 이 항목은 기록만 하고 판정하지 않는다.
그 외 축(`scoring_method` 기본 `sequence`, 추론 경로 어휘 로드)은 CLAUDE.md §2와 일치한다.

### 영향
수정: `decisions/DESIGN_LOG.md`(이 항목 추가 + 아래 `[2026-09-11]` 항목의 사실관계 정정),
`CLAUDE.md:91`(각주 문구만 — 정본 우선순위 서술과 `실험계획_추론경로_260824.md` 참조 표기는 무변경).
`docs/experiments/experiment_design_v0.1.md`는 무변경. 코드·스키마 변경 없음. 커밋하지 않음.

---

## [2026-09-11] `실험계획_추론경로_260824.md` 실체 확인 — main에 `docs/experiments/experiment_design_v0.1.md`로 반영
> **Task**: multi

**배경**: 바로 아래 `[2026-09-11] 파이프라인 현재 문제점 종합 목록` 항목의 15번("실체 부재",
`CLAUDE.md:91` 각주 처리만 완료)과 `[2026-08-31] 재현성 불일치 조사` 항목의 §6("근거 추적
불가")이, CLAUDE.md §2 정본 우선순위 3번째 문서 `실험계획_추론경로_260824.md`가 main
워킹트리 어디에도 없다고 기록해뒀다. 지난 세션 각주는 "worktree
`.claude/worktrees/selection-dist/docs/experiments/experiment_design_v0.1.md`가 같은
내용으로 추정되나 파일명이 달라 동일 문서 여부는 미확인"으로 열어둔 상태였다. 이번
세션에서 해당 worktree 파일을 직접 읽어 제목이 "실험 계획 — 추론 경로 및 모델 대조군
(2026-08-24)"로 **정확히 일치**함을 확인했다. 그 파일은 worktree 브랜치에도 커밋되지
않은 loose 파일이었다.

**변경**:
- main `docs/experiments/experiment_design_v0.1.md`를 신설. **[2026-09-14 정정]** 최초
  생성은 worktree 판(2026-08-24, 5-arm) 내용으로 했으나, **사용자가 이후 main 파일을 직접
  편집·저장**해 최종본은 `실험 설계 정본 v0.1`(A1~A4 4-arm)이다. 따라서 이 항목의 최초
  서술 "내용 무변경으로 복사(`diff` 결과 바이트 동일 확인)"는 **사실과 달라 철회한다** —
  두 판본은 내용이 다르다. 사용자가 이 최종 저장본을 정본으로 확정(2026-09-14, 위
  `[2026-09-14] experiment_design_v0.1.md 정본 확정` 항목 참고). 파일명은
  `decisions/DESIGN_LOG.md:731`(구 번호, 이 항목 삽입 전 기준)이 이미 이 이름으로
  지칭하고 있어 그대로 유지 — 정본 우선순위 서술의 `실험계획_추론경로_260824.md`라는
  참조 표기 자체는 CLAUDE.md에서 안 바꾼다(그 표기는 여전히 "정본으로 지목하는 문서"의
  이름이고, 이 문서가 그 실체임을 각주가 가리킨다)
- `CLAUDE.md:91` 각주를 "저장소 어디에도 실체 없음 — 부재 ... worktree ... 추정되나
  파일명이 달라 동일 문서 여부는 미확인"에서 "확인 완료 — `docs/experiments/
  experiment_design_v0.1.md`로 메인에 반영됨(2026-09-11)"으로 정정.
  **[2026-09-14 재정정]** 이 각주는 "부재" 서술과 "확인 완료" 서술이 한 줄에 공존해
  모순이었다. 아래 B 항으로 모순 문구를 제거하고 정본 확정 사실로 대체함
- worktree 원본은 그대로 둠(삭제·이동 없음)

**이 정정이 해소하지 않는 것**: `[2026-08-31] 재현성 불일치 조사` §6 "근거 추적 불가"의
본체 — `versioning.reproducibility_params` 5군을 기록하는 코드가 아직 없다는 사실,
그리고 이 실험계획 문서 자체가 "기존에 확인된 5-vote 비결정성"의 **원 관측**(날짜·수치·
해시 비교)을 담고 있지 않다는 사실 — 은 **미해소로 유지**된다. 문서 실체를 확인했다고
해서 그 안에 없는 실측 근거가 생기지는 않는다. 실제로 이 문서(실험 A~D)는 지침서 본문에
넣지 않은 **미검증 가설의 실험 설계**이지, 과거 관측 기록이 아니다 — §6이 찾던 것과는
다른 종류의 문서라는 점도 이번에 내용을 대조해 확인했다.

**미결 갱신**: `[2026-09-11] 파이프라인 현재 문제점 종합 목록` 15번("실체 부재")은 이
항목으로 **해소됨**. `[2026-08-31]` §6 "근거 추적 불가"는 **미해소 유지**(위 근거).

**충돌 확인**: 새 설계 결정 아님 — 기존 미결 항목의 사실관계(문서 소재) 확인·반영이며,
기존 결정과 충돌하지 않는다.

### 영향
신규: `docs/experiments/experiment_design_v0.1.md`. **[2026-09-14 정정] "worktree loose
파일과 바이트 동일"은 사실과 다르므로 삭제** — 현재 내용은 사용자 최종 저장본
`실험 설계 정본 v0.1`이다.
수정: `CLAUDE.md:91`(각주만, 정본 우선순위 서술 자체는 무변경), `decisions/DESIGN_LOG.md`
(이 항목 추가). 코드·스키마 변경 없음. 커밋하지 않음(워킹트리 유지).

---

## [2026-09-11] exp 네임스페이스 분리 구현 — outputs/episodes·labels 사이클 간 덮어쓰기 방지
> **Task**: multi

**배경**: 바로 아래 `[2026-09-11] 파이프라인 현재 문제점 종합 목록` 항목의 1번
(`outputs/episodes/`·`outputs/labels/` 비영속화, exp 네임스페이스 없음 — 재실행마다 이전
사이클의 클립별 중간 산출물이 덮어써지는 문제, C001b·C001c 산출물을 "C001 결과"로
오귀속한 실측 사례로 발견)에 대해 사용자가 처리 착수를 승인해 오늘(2026-09-11) 구현을
완료했다.

### 목표 경로 구조
- `outputs/episodes/<clip>.json` → `outputs/episodes/<exp>/<clip>.json`
- `outputs/labels/<clip>/{s2,s3,final}.json` → `outputs/labels/<exp>/<clip>/{s2,s3,final}.json`

### 생산 측 (pipeline-integrator)
- `task_episode/run_pipeline.py`: `audit_clip()`(66행 함수 정의)에 `exp` 파라미터 추가,
  경로 조립 2곳에 `exp` 삽입 — episodes 기록(140행:
  `_write_json(ROOT / "outputs" / "episodes" / exp / f"{clip_id}.json", episodes_doc)`),
  labels 기록(152~154행: s2/s3/final 3개 경로 전부 `outputs/labels/exp/clip_id/...`로 조립).
  모듈 docstring(10~19행)·실행 안내(20행)·요약 출력 문구(256행)도 새 경로로 갱신
- `task_episode/render_c001_overlay.py`: `--exp` 필수 인자 추가(86행, `verification/gate.py`와
  동일 패턴), `main()`에서 `labels_dir = ROOT / "outputs" / "labels" / args.exp`(91행)로
  지역 계산 — 이전에 있던 `LABELS_DIR` 모듈 상수 하드코딩 제거
- `docs/design/AGENT_DESIGN.md` §3 표(68~69행, `outputs/episodes/<exp>/<clip>.json`·
  `outputs/labels/<exp>/<clip>/{s2,s3,final}.json`로 갱신), `.claude/agents/
  pipeline-integrator.md`의 "계약 경로 영속화" 서술(12행)도 같은 방식으로 갱신
- 검증: `--set gold50 --exp NSTEST --n 2 --workers 2` 스모크 실행으로 새 경로에 정상 생성
  확인 + 기존 flat 경로(`outputs/episodes/*.json` 등)는 이 실행으로 전혀 안 건드려짐을 확인
  → 검증 후 NSTEST 잔여물 삭제

### 검증 측 (verifier)
- `verification/gate.py`: `_load()`(38~43행)·`gate_clip()`(59행)에 `exp` 파라미터 추가,
  경로 조립에 삽입(39~40행: `ROOT / "outputs" / "episodes" / exp / f"{clip_id}.json"`·
  `ROOT / "outputs" / "labels" / exp / clip_id / "final.json"`). 판정 로직(reachable·
  direction_consistent 계산 등, 76~118행)은 무변경 — `main()`의 `--exp` 인자(128행)로 받아
  `gate_clip(cid, args.exp)`(135행)에 전달만 함
- `verification/score_gold.py`: `_predicted_episodes()`(42~54행)에 `exp` 파라미터 추가,
  경로(43행: `ROOT / "outputs" / "episodes" / exp / f"{clip_id}.json"`)에 삽입. 채점 로직
  (`_match`(57행)·`score_clip`(69행) 등)은 무변경 — `main()`의 `--exp` 인자(105행)로 받음
- `verification/report.py`: `_contract_table()`(42행)·`_completion_coverage()`(82행)에
  `exp` 파라미터 추가, 표 라벨 문자열(45~52행 계약 경로 행)도 실제 경로(`outputs/episodes/
  <exp>/...`·`outputs/labels/<exp>/...`)와 일치하도록 정정. 집계 로직은 무변경
- 검증: `--set gold50 --exp NSVERIFY --n 3 --workers 2`로 신선 데이터 생성 →
  `gate.py --exp NSVERIFY`·`score_gold.py --exp NSVERIFY`·`report.py --exp NSVERIFY` 3개
  전부 새 경로에서 정상 동작 확인(§1 계약 경로 표 카운트 3/3 정상 산출) → 검증 후
  NSVERIFY 잔여물 삭제

### 기존 데이터 처리
- 기존 flat 데이터(`outputs/episodes/*.json` 50개, `outputs/labels/*/` 50개 — C001b/C001c가
  혼재해서 쓴 것)는 **삭제하지 않고 그대로 둠**. 이 변경 이후 어떤 스크립트도 이 경로를
  읽거나 쓰지 않으므로 고아 파일로 남는다. 삭제 여부는 이번 작업 범위 밖, 사람 판정 대기
  상태 유지
- **한계**: 이 변경은 향후 재실행부터 데이터 소실을 막을 뿐, 이미 소실된 원본 C001의
  클립별 중간 산출물(9/9 15:29~15:37 판정 원문)은 복구되지 않는다

### 영향
수정: `task_episode/run_pipeline.py`·`task_episode/render_c001_overlay.py`·
`verification/gate.py`·`verification/score_gold.py`·`verification/report.py`(코드 5개
파일, 위 요약 참고) + `docs/design/AGENT_DESIGN.md`·`.claude/agents/pipeline-integrator.md`
(문서 2개). 판정 로직(게이트 5술어·gold 매칭·리포트 집계)은 전부 무변경 — 경로 조립부에
`exp` 인자만 추가. 커밋하지 않음(워킹트리 유지).

---

## [2026-09-11] 파이프라인 현재 문제점 종합 목록 (우선순위순)
> **Task**: multi

**배경**: 2026-09-09~10 사이클(C001) 완료 판정 확정 이후, 사용자가 C001 결과를 오버레이
영상으로 직접 확인하는 과정에서 신규 구조적 결함을 발견했다. "C001 결과"라며 렌더링한
오버레이 영상 3개 중 2개는 C001b, 1개는 C001c 산출물이었다 — mtime으로 확인:
`outputs/labels/f1ee5309-.../final.json`은 17:33:53(C001b 실행창), `outputs/labels/
0368ee92-.../final.json`은 17:36:37(C001c 실행창), `outputs/labels/a0a0a9c2-.../
final.json`은 17:32:27(C001b 실행창). 원본 C001은 9/9 15:29~15:37에 끝났으므로 셋 다
원본이 아니다. 원인은 `outputs/episodes/`·`outputs/labels/`가 `--exp` 인자와 무관하게
항상 같은 경로에 쓰여, 재실행(C001b·C001c)마다 이전 사이클의 클립별 중간 산출물을
덮어쓰기 때문이다. 이 신규 발견을 계기로, 현재 파악된 전체 문제 15개를 우선순위순으로
한 자리에 모아 기록한다. 이하 2~13·15번은 이미 2026-09-09 또는 2026-09-10 항목에
상세 기록돼 있으므로 중복 서술하지 않고 링크만 건다. 1번(신규)과 14번(이번에 처음 등재)만
근거를 풀어 쓴다.

### 15개 항목

1. **`outputs/episodes/`·`outputs/labels/` 비영속화(exp 네임스페이스 없음)** — 신규
   (2026-09-11 발견), 최우선. `task_episode/run_pipeline.py:139`(`outputs/episodes/
   <clip>.json` 기록)와 `:151-153`(`outputs/labels/<clip>/{s2,s3,final}.json` 기록)가
   `--exp` 인자와 무관하게 항상 같은 경로에 쓴다(직접 읽어 재확인 — 경로 리터럴에 `exp`
   변수가 전혀 개입하지 않음). 반면 `experiments/results/<exp>/`(report.json·gate 결과·
   gold_score·manifest.json)만 `exp` 문자열로 네임스페이스돼 있다(`main()`의
   `experiments/results/{exp}/` 경로 조립부 참고). 그 결과 재실행마다 이전 사이클의
   클립별 중간 산출물(에피소드 후보 전량, S2/S3 raw, final 결정)이 소실되고, 남는 건
   집계 리포트뿐이다 — 위 배경에서 설명한 오버레이 영상 오귀속 사례가 그 직접적 증거다.
   §2 "산출물에 반드시 남길 것 — 중간 산출물(S2 출력) 영속화, 후보 집합 전체 보존"
   설계 원칙이 **사이클 간에는 깨진다**(단일 사이클 내에서는 지켜짐 — S2/S3 분리, 후보
   전량 보존 자체는 정상). `experiments/results/<exp>/`만 보고 "이 사이클 결과"라고
   믿을 수 있는 것은 집계 수치뿐이고, 클립 단위로 "그 사이클에 실제로 무엇이 나왔는지"
   재구성할 방법이 현재 코드에는 없다. 처리 착수 여부는 사람 판정 대기(미결로도 등재).

2. Track1 `cause` 값 재현 불안정(6/50, `TEMPERATURE=0`인데도 흔들림) —
   2026-09-10 항목(§재현성 2회 대조) 참고.

3. 물리 게이트 실효 커버리지 2/5, `reachable` fail 32/60(53%) — 2026-09-09 항목(§물리
   게이트가 신규 발견을 냈다) 참고. DV-7(`cause_source`)로 해소 예정 — 2026-09-10 항목
   (§DV-7 채택 결정)의 채택 결정도 함께 참고.

4. `ERR-ATTRIB`(인과귀속) 채점 불가 — gold.json에 cause 필드 없음 — 2026-09-09 항목
   (§변경, `verification/` 신설 절) 참고. 근거 필드는 `verification/score_gold.py:144`의
   `err_attrib_note`("ERR-ATTRIB(S3 인과귀속) 채점 불가 — gold.json에 cause 필드 없음
   (스키마 갭)").

5. 무기록 fallback 잔존 — map_valid=false 33/50, `fallback_path` 플래그 미기록(강제규칙
   4 위반) — 2026-09-09 항목(§무기록 fallback 잔존 확인) 참고.

6. precision 하한만 측정 가능(0.144), Phase D(완전라벨 gold) 미구현 — 2026-09-09 항목
   (§gold 채점이 기존 문서 수치를 독립 재현) 참고.

7. vocab v0.4 전환 미착수 — 목표 필드 9개 0/60, cause 4→6값 미전환 — 2026-09-09 항목
   (§목표 완수도) 참고.

8. pre-commit 훅 2개 구조적 결함(`check_import_separation`, `check_experiment_card`) —
   2026-09-10 항목(§pre-commit 6훅 전수 판정, 신규 발견 2건) 참고.

9. `task_episode/tag_v08.py` 기존 부채(임계 리터럴 `LEAD_IN=3.0`, 어휘 값 리터럴 8개,
   pre-commit이 탐지했으나 미수정) — 2026-09-10 항목(§pre-commit 6훅 전수 판정) 참고,
   최초 발견은 2026-09-09 항목(§변경).

10. DV-7 구현 미착수(채택은 완료, C002 대기) — 2026-09-10 항목(§DV-7 채택 결정, §C002
    착수 조건) 참고.

11. `selected50` 기준 회귀 실행 미실행(C001은 gold50만 실행) — 2026-09-09 항목(§미결)과
    2026-09-10 항목(§미결, "2026-09-09 항목에서 이월") 참고.

12. 곡률보정 무보정 호출부 6곳(`classify073.py`·`taxo_detect.py`·`vlm_verify.py`·
    `map_lane.py`·`selection.py`·`folder_selection.py`) — 2026-09-09 항목(§미결) 참고,
    최초 상세는 2026-09-08 항목(Track2 곡률보정 배선).

13. `s2_conditioning` 문서-실측 불일치(스키마 기본값 `minimal` vs 실측 `rule_injected`,
    GT 카테고리 힌트 상시 주입) — 2026-09-09 항목(§변경, `report.py`의 "스키마-실측
    불일치" 체크 절) 참고. 최초 발견·상세 기록은 2026-08-28 항목(§공개의무 배선).

14. **`conflict_register` pending 2건** — `common/schema/tag_vocab_v0.4.json:1357`
    (`conflict_register`) 아래 `pending`(:1363-1366)에 다음 두 건이 있고, 이번에
    DESIGN_LOG에 처음 등재한다.
    - `C2_원인_축_귀속`(:1364): "target_axis 미표기 유지 여부 — KPI-18-5 Causal-F1
      채점 단위에 직접 영향. KPI 정의서 대조 후 결정." 없으면 무엇이 불가능한가:
      인과 원인이 필드축(cause_type 등) 중 어디에 귀속되는지 표기가 없으면 KPI-18-5
      Causal-F1을 "필드 단위로" 채점할지 "전체 인과 판단 단위로" 채점할지가 코드마다
      다르게 정해질 수 있고, 이는 verifier의 채점 로직과 gold 라벨링 스키마 양쪽에
      영향을 준다. 기존 결정과 충돌 여부: `conflict_register.resolved`의
      `C1_ego_종횡`(필드는 분해 저장, 사건은 단일 — 현행 `ego_action` 유지)과 유사한
      "분해 vs 단일" 성격의 미결이나, C1은 이미 해소됐고 C2는 원인 축에 대해 같은
      질문이 아직 열려 있다 — 직접 충돌은 아니지만 같은 계열의 미해소 잔여 항목.
    - `C5_relevance_vs_influence`(:1365): "relevance 3값이 구 criticality·
      influence_level을 대체 가능한지 — KPI-18-3 Influence-κ 대응 확인 필요." 없으면
      무엇이 불가능한가: 구 계보(criticality·influence_level, 2값/서수 체계로 추정)와
      신 계보(relevance 3값)의 대응표가 없으면 KPI-18-3 Influence-κ(구 계보 기준
      설계된 것으로 보이는 지표)를 신 어휘 산출물에 그대로 적용할 수 있는지 판정할
      수 없다 — 잘못 대응시키면 값 집합 크기가 다른 두 척도 간 κ 계산이 무의미해진다.
      기존 결정과 충돌 여부: `resolved`의 `C4_environment`(맥락 전용, road_geometry·
      road_condition은 별도 cause_type — 이중 계상 방지)와 같은 "구 계보 흡수 시
      이중 계상 금지" 원칙이 이미 있으므로, C5 해소 시에도 relevance가 구
      criticality·influence_level을 이중으로 흡수(같은 개념을 두 곳에 남기는 것)하지
      않도록 그 원칙을 적용해야 한다 — 직접 충돌은 아니나 해소 시 지켜야 할 선행
      결정.
    두 건 모두 판정 주체는 KPI 정의서 대조 후 사람(또는 verifier)이며, 이 항목은
    미결로만 남기고 판정하지 않는다.

15. `실험계획_추론경로_260824.md` 실체 부재 — 저장소 어디에도 없음, `CLAUDE.md:91`
    각주 처리만 완료된 상태 — 2026-09-10 이전부터 알려진 사실. `decisions/
    DESIGN_LOG.md`의 2026-08-31 항목(재현성 불일치 조사, §6 "근거 추적 불가",
    `실험계획_추론경로_260824.md`는 main 워킹트리에 없다는 기록) 참고. 참조만.
    **해소됨 — 위 신규 항목([2026-09-11] `실험계획_추론경로_260824.md` 실체 확인)
    참고.** §6 "근거 추적 불가"의 나머지 부분(원 관측 부재)은 그 항목에서도 미해소로
    유지됨.

### 미결
- **1번(exp 네임스페이스 분리)을 신규 최상위 미결 항목으로 추가**: `outputs/episodes/`·
  `outputs/labels/`를 `--exp`로 네임스페이스하거나, 최소한 재실행 전 이전 산출물을
  아카이브하는 처리가 필요하다는 것이 이번에 실측으로 확인됨. **해소됨 — 아래 신규
  항목([2026-09-11] exp 네임스페이스 분리 구현 — outputs/episodes·labels 사이클 간
  덮어쓰기 방지) 참고.**
- 나머지 항목(2~13, 15번)은 기존 미결 목록(2026-09-09/10 항목)에 이미 있으므로 위
  15개 목록의 각 링크를 참고. **15번은 위에서 해소됨으로 갱신.**

### 영향
문서만 수정(`decisions/DESIGN_LOG.md`). 코드·스키마 변경 없음. 커밋하지 않음(이번
세션 관행 유지, 사용자 지시).

---

## [2026-09-10] C001 완료 판정 확정 + DV-7(`cause_source`) 채택 결정 — 구현은 C002로 이관
> **Task**: multi

**배경**: 2026-09-09 사이클(C001, gold50 50클립 완주)의 완료 검토가 끝났다. 사람(사용자)이
2026-09-10에 다음 세 가지를 결정했다: (1) 완료 기준 미해소분(재현성 2회 대조, pre-commit
6훅 전수 확인) 해소 진행 (2) 미승인 제안서 DV-7(`experiments/proposals/
2026-09-09_dv7-cause-source.md`) 채택 (3) 커밋하지 않음(워킹트리 유지). 이 항목은 그 결정과
해소 작업 결과를 기록한다.

### C001 완료 기준 5개 — 최종 판정
- **① 계약 경로 실체화 + gold50 실행**: 충족(2026-09-09 항목에서 이미 확인)
- **② pre-commit 6훅 전수 확인**: **부분 충족 + 신규 결함 기록**으로 종결(아래 상세)
- **③ 리포트 수치화**: 충족(2026-09-09 항목, `reports/C001.md`)
- **④ 재현성 2회 대조**: **충족, 불일치 원인 규명**으로 종결(아래 상세)
- **⑤ 게이트 pass 없음(fail/undetermined만 산출)**: 충족(설계상 정상, 2026-09-09 항목에서 확인)

### 재현성 2회 대조 (기준 ④)
`./run.sh task_episode/run_pipeline.py --set gold50 --exp C001b --workers 4` 재실행,
50/50/50 성공, wall-clock 501.0s.
- `experiments/results/C001/manifest.json`과 `C001b/manifest.json`의 `run.report_hash`
  불일치(C001: `b5aed702...`, C001b: `75976120...`)
- `elapsed_s` 제외 실질 diff: `s0.*`(CAN 규칙 앵커) **50/50 완전 일치**(설계 원칙 1 "앵커
  비오염" 재확인) / `track1.cause_by_segment` **6개 clip에서 변동**(`TEMPERATURE=0`인데도
  흔들림 — manifest의 `decoding.seed`가 두 실행 모두 null이라 "seed 고정만으로는 재현되지
  않는다" 원칙의 실측 사례) / `track2.n_tags_total` 다수 clip 변동(VLM 5-vote temp>0
  설계상 정상)
- 판정: 해시 불일치는 실패가 아니라 원인이 규명된 결과 → 기준 ④ **충족**으로 종결

### pre-commit 6훅 전수 판정 (기준 ②)
`git add -A` → `pre-commit run --all-files` → `git reset`(워킹트리 내용 보존 확인)으로
6훅 전부 실검사 확인:
- `check_no_literal_thresholds`·`check_vocab_duplication` — `task_episode/tag_v08.py`의
  기존 부채(임계 리터럴 `LEAD_IN=3.0`, 어휘 값 리터럴 8개) 실제로 탐지·차단 확인(미수정,
  판정 로직이라 범위 밖)
- `check_gold_isolation`·`check_vocab_sync` — 정상
- **신규 발견 — `check_import_separation`**: 형식상 Passed지만 probe 테스트로 탐지 실패
  확인. 이 저장소의 flat import 관행(`run.sh`의 PYTHONPATH 평탄화로 `import tag_v08`처럼
  모듈명 직접 import)과 훅의 디렉토리 basename 비교 로직이 맞지 않아 실제 위반을 탐지할
  수 없는 구조적 결함
- **신규 발견 — `check_experiment_card`**: 형식상 Passed지만 `.gitignore:25`가
  `experiments/results/`를 통째로 무시해 정상 `git add -A` 워크플로우로는 이 훅의 대상이
  절대 스테이징되지 않는 구조적 결함(강제 스테이징 probe로만 로직 정상 확인)
- 판정: 6훅 모두 "대상 없음 침묵"은 해소됐으나 2개 훅(import-separation, experiment-card)은
  실효 탐지력이 없는 구조적 결함이 새로 발견됨 → 기준 ② **"부분 충족 + 신규 결함 기록"**으로
  종결

### 매니페스트 리터럴 정정 (부수 작업, B6)
`task_episode/run_pipeline.py`의 `MF.build()` 호출에서 `frame_rate=10`·`resolution=1280`
리터럴을 제거해 `common/manifest.py`의 기본값 경로(`config.SEND_FPS`·
`config.WINDOW_MAX_SIDE`)를 타도록 수정. 미사용 `import copy` 없음 확인(원래부터 없었음).
`./run.sh task_episode/run_pipeline.py --set gold50 --exp C001c --n 2 --workers 2`로
검증(정상 완주, manifest.json의 frame_rate/resolution이 config 경유로 확인됨).

### DV-7 채택 결정 — `cause_source` 필드
`experiments/proposals/2026-09-09_dv7-cause-source.md`의 제안(레코드에
`cause_source ∈ {gt_confirmed, model_reported}` 필드 추가 — `tag_v08.py`의 `in_path`
truthy 분기면 `gt_confirmed`, else 분기(모델 자유분류 채택)면 `model_reported`)을
**채택한다**.
- **채택 근거**: 판정 로직·enum·임계 불변, 이미 계산된 `in_path` 분기 결과를 기록만 하는
  변경 — 무기록 fallback 금지 원칙(설계 원칙 4)과 동일 성격의 보완
- **보강 근거(이번 사이클 신규 관측)**: 위 재현성 2회 대조에서 `track1.cause_by_segment`가
  6개 clip에서 흔들리는 것이 관찰됨. cause 값 자체가 재현 불안정하다면, 어느 경로(GT
  확정/모델 보고)로 나온 값인지 구분하는 `cause_source`의 필요성이 더 커진다 — 흔들림이
  `model_reported` 경로에 쏠려 있는지는 이번엔 층화 확인 전이라 미검증이며, C002의
  층화 채점으로 확인 대상
- **구현은 이번 사이클에서 하지 않는다** — C002로 이관(아래 착수 조건)

### 미결(다음 사이클 후보)
- `check_import_separation`·`check_experiment_card` 구조적 결함 수정 — 변경축 후보로
  등록, 착수 여부는 사람 판정 대기
- (2026-09-09 항목에서 이월, 미해소) 곡률보정 무보정 호출부 6곳, cause 4→6값 전환,
  vocab v0.4 전면 전환, `selected50.json` 기준 회귀 실행

### C002 착수 조건
- 변경축 1개만: DV-7(`cause_source` 필드) — §4 전환 규칙 "1 브랜치 = 1 변경 축" 준수
- 대조 기준선: C001 / C001b
- 구현 범위: `task_episode/tag_v08.py`의 cause 산출 블록에 `cause_source` 필드 부착(판정
  로직·enum·임계 불변) + `verification/gate.py`의 `reachable` 검사가 `cause_source`
  분기를 반영하도록 확장(새 필드를 읽는 것만 추가, 임계는 안 바꿈)
- gold50 재실행 후 **층화 채점**(`gt_confirmed` vs `model_reported` 그룹별 별도 정확도·
  게이트 통과율 보고) — DV-7 제안서 §4 가설·§6 반증 조건 그대로 적용

### 영향
수정: `decisions/DESIGN_LOG.md`(이 항목 추가). 코드 변경 없음 — 매니페스트 리터럴 정정은
이미 별도로 적용·검증됐고 이 항목은 그 사실을 기록만 함. DV-7 구현은 미착수이며 다음
사이클(C002) 항목에서 코드 diff와 함께 기록될 예정. 커밋하지 않음(워킹트리 유지, 사용자
지시).

---

## [2026-09-09] 에이전트 자율 루프 배선 — 계약 경로 실체화 + pipeline-integrator 신설 + 첫 사이클(C001)
> **Task**: multi

**배경**: "task를 정의하면 에이전트가 결과를 확인해 가며 최적화를 진행하고, 사람은 리포트
리뷰로 결정만 하는" 체제를 원한다는 요청. 직전 엔트리(①agent 설계상 중복 오류)가 이미
지적했던 "인수인계 파일 계약이 실제와 다른 세계를 전제한다"는 미해결 항목을 이번에 메웠다
— 배선 없이는 "각 에이전트가 역할대로 동작하는지"를 50클립 재실행만으로는 관찰할 수
없다는 점을 먼저 확인(계약 경로 6곳 전부 미실재, verifier가 채점할 코드 0줄, P/R 계산
코드 git 이력에도 없음)한 뒤 배선을 만들고, 그 배선으로 gold 50클립 1사이클을 도는 것
자체를 검증으로 삼았다. 상세 계획: `~/.claude/plans/agent-fluttering-gosling.md`.

### 변경
- **계약 경로 실체화**: `outputs/episodes/`·`outputs/labels/`·`experiments/{cards,results,
  proposals}/`·`reports/`·`verification/` 디렉토리 생성(`AGENT_DESIGN.md` §3 표와 동일 경로).
  `hooks/hook_utils.py`의 `VERIFY_DIRS`·`CARDS_DIR`·`RESULTS_DIR`는 경로 자체는 이미 맞았고
  (직전 엔트리에서 정정), 디렉토리 실재만 없었다 — 이번에 실재하게 되어
  `check_import_separation`·`check_experiment_card` 2개 훅이 "대상 없음" 침묵을 벗어나
  실제로 검사를 수행함을 `pre-commit run --all-files`로 확인(임시 스테이징 후 언스테이징,
  커밋 안 함). 그 과정에서 `task_episode/tag_v08.py`의 **기존 리터럴 위반 2건**(로컬
  `LEAD_IN=3.0`, 어휘값 8개 리터럴)이 실제로 걸리는 것도 확인 — 이번 변경 범위 밖(이미
  `thresholds.py` 주석에 "건드리지 않음"으로 문서화된 기존 부채)이라 손대지 않았다
- **`pipeline-integrator` 에이전트 신설**(10번째, `.claude/agents/pipeline-integrator.md`):
  rule-engineer(S0+S1)·vlm-engineer(S2+S3)의 산출물이 실제로는 `tag_v08.tag_clip_v08()`
  한 함수에 융합돼 관찰 불가능했던 문제를, 로직을 바꾸지 않고 반환값을 계약 경로로
  내보내는 배선만 담당하는 별도 축으로 분리. `AGENT_DESIGN.md` §1~§4·§6에 반영
- **`task_episode/run_workflow_audit.py` → `task_episode/run_pipeline.py` 승격**: 기존
  카운트 요약(S0/Track1/Track2 성공률·CJK·파싱실패·arc불일치)은 유지하고, clip마다
  `outputs/episodes/<clip>.json`(S0+S1 후보 전량, 미선택 포함)·`outputs/labels/<clip>/
  {s2,s3,final}.json`(S2/S3 분리)·`experiments/results/<exp>/manifest.json`을 추가로 쓴다.
  `--set gold50|selected50` 인자로 Stage1(`selected50.json`)과 gold(`sample_clips.json`,
  gold.json 보유 50)를 명시적으로 분리 — 두 세트는 교집합 0이라 섞어서 하나의 분포로
  보고하지 않는다(가이드 §1.5)
- **`tag_v08.py` 최소 개입**: `_reason()`(S2) 출력과 `_structure()`(S3) 스냅샷을
  `rec["_s2_raw"]`/`rec["_s3_raw"]`로 추가만 함(4줄) — 시그니처·판정 로직 불변
- **`common/manifest.py` 신설**: 재현성 매니페스트 5군(모델/서빙/디코딩/입력/데이터).
  체크섬·dtype·tensor_parallel 등 NIM 서빙 API가 노출하지 않는 항목은 추정하지 않고
  `"unavailable:<사유>"`로 명시 기록(무기록 fallback 금지 원칙 적용)
- **`verification/` 신설**(gate.py·score_gold.py·report.py) — 파이프라인 모듈 import
  없이 `outputs/`의 JSON만 읽음(check_import_separation 요건):
  - `gate.py`: 물리 게이트 5술어(`verifier.md`가 CLAUDE.md 6검사 중 geometric_consistency+
    proximity_range를 reachable로 통합한 설계를 그대로 구현). 산출은 fail/undetermined만
  - `score_gold.py`: gold 50 채점. `tmp/run_demo_diag.py::match_gold()`(최대겹침 매칭)
    확장 — recall은 신뢰 가능, precision은 sparse gold(absent 라벨 없음)라 **하한만**
  - `report.py`: `reports/<exp>.md` 자동 생성 — 계약 경로 현황 + 앵커 오염 정적 확인 +
    Track1/2 불일치 + 무기록 fallback + 스키마-실측 불일치 + 목표 완수도 + 게이트/gold
    요약 + 전 사이클 회귀
- **사이클 구조 도입**(`AGENT_DESIGN.md` §6, `CLAUDE.md` "에이전트 위임" 절): task 정의
  → 카드 자동등록(실행 전, 타임스탬프 고정) → 실행·영속화 → 게이트+채점+회귀 → 제안서
  → **사람 승인은 사이클 끝 1곳만** → 결정 기록. 강제규칙 3(사전 등록 없는 채점 거절)과의
  정합은 "카드가 실행 전 자동 생성·타임스탬프 고정"으로 형식 유지, 사람 개입 시점만
  뒤로 이동한 것으로 처리

### 검증 — C001 (gold 50, workers=4)
`./run.sh task_episode/run_pipeline.py --set gold50 --exp C001 --workers 4` → S0/Track1/
Track2 전부 50/50 성공, CJK 1건, 파싱실패 0, arc 불일치 0/50, map_valid 17/50,
wall-clock 485.8s(8.1분). 상세: `reports/C001.md`.

- **gold 채점이 기존 문서 수치를 독립 재현**: `recall=0.821` (CLAUDE.md §1 기존 기재
  "OR합집합 0.81"과 근사 일치) — 별도로 새로 짠 매칭 로직이 기존 ad-hoc 측정과 정합적이라
  score_gold.py의 신뢰도에 대한 교차 확인이 됨. `precision_lower_bound=0.144`는 recall-우선
  OR 합집합 설계(candidates.py 주석 "precision은 Phase C"와 일치하는 결과)상 예상된 낮음이며
  하한으로만 보고(sparse gold라 실제 precision은 더 높을 수 있음)
- **물리 게이트가 신규 발견을 냈다**: `reachable` 검사 fail=32/60(53%) — 전부 `cause=
  "agent"`로 확정된 세그먼트인데 `critical_components`에 GT 거리 계측(`ref.distance_m`)이
  전혀 없는 경우. 원인 추적 결과 `tag_v08.py:270-277`의 cause 산출이 두 경로(GT
  `in_path` 존재 → "agent" / GT 없음 → 모델 자유분류 `v.get("cause")`도 "agent"일 수 있음)
  를 **구분 없이 같은 문자열로 합류**시키기 때문 — 지금까지 어떤 리포트에도 없던 발견.
  제안서로 남김: `experiments/proposals/2026-09-09_dv7-cause-source.md`(DV-7, `cause_source`
  필드 추가, 판정 로직 불변)
- **목표 완수도**: 예상대로 미달 확인 — `transition_filters_passed`·`curvature_correction_
  source`·`merged_from`·`window_start_reason`·`window_end_reason`·`threshold_set_id`·
  `physical_gate_passed`·`camera_visible`·`episode_confidence` 전부 0/60. cause 4값
  vs 목표 6값(정적 사실). 이번 사이클에서 메우지 않음 — 다음 사이클 변경축 후보로 남김
- **무기록 fallback 잔존 확인**: map_valid=false 33/50인데 `fallback_path` 플래그가
  산출물 어디에도 없음 — 기존 갭, 이번 범위 밖(별도 변경축)

### 영향
신규: `.claude/agents/pipeline-integrator.md`, `common/manifest.py`, `verification/
{gate,score_gold,report}.py`, `task_episode/run_pipeline.py`, `experiments/proposals/
2026-09-09_dv7-cause-source.md`. 수정: `docs/design/AGENT_DESIGN.md`(§1~§4·§6),
`CLAUDE.md`(에이전트 위임 절 신설), `hooks/hook_utils.py`(주석·GOLD_ALLOWED 정정),
`common/thresholds.py`(`GATE_PROXIMITY_MAX_M` 추가), `.gitignore`(`experiments/results/`·
`reports/*.md` 추가), `task_episode/tag_v08.py`(4줄, 판정 로직 불변). 삭제:
`task_episode/run_workflow_audit.py`(run_pipeline.py로 승격, 기능 상위집합). 전부
미커밋 — 사용자 지시 대기(이 저장소 관행상 기본적으로 커밋은 명시 요청 시에만).

### 미결(다음 사이클 후보)
- `experiments/proposals/2026-09-09_dv7-cause-source.md` 승인 여부 — 물리 게이트 reachable
  검사의 실효성이 여기 달림
- 곡률보정 무보정 호출부 6곳(`classify073.py:183` 등) — 여전히 범위 밖
- cause 4→6값 전환, vocab v0.4 전면 전환 — 별도 변경축
- `selected50.json`(Stage1 top-50) 기준 회귀 실행 — C001은 gold50만 실행, 회귀 기준선
  미실행

---

## [2026-09-08] Track2 곡률보정 배선 — Track1/Track2 arc 불일치 해소
> **Task**: episode(Track2)

**배경**: 직전 엔트리("NVIDIA 데이터셋 전체 workflow 최초 실행")에서 50개 중 1개 clip
(`024415a5-94a5-4810-9c27-a72314b55146`)이 Track1엔 `lane_change_right`가 있고 Track2엔
없는 걸 실측 확인함. 원인은 `task_episode/candidates.py:111`이 `events.detect_events()`를
곡률보정 없이 호출하는 것(`tag_v08.py:223`은 2026-08-28부터 보정 적용 중이었음 — 7개
무보정 호출부 중 하나였음).

**변경**: `candidates.py`에 `map_lane.default_curvature_fn(clip_id, dur)`를 계산해
`detect_events(clip_id, curvature_fn=curvature_fn)`로 전달 — `tag_v08.py`와 동일 배선.

**검증**: 불일치가 났던 그 clip 하나로 재실행 → Track1/Track2 arc가
`['accelerate','decelerate','lane_change_right','stop']`로 **완전 일치**함을 확인(이전엔
Track2에서 `lane_change_right` 누락). 나머지 49개 clip 전체 재실행은 비용상 이번엔
생략 — 필요시 `./run.sh task_episode/run_workflow_audit.py --n 50`로 재확인 가능.

**영향**: `task_episode/candidates.py`(커밋 여부 미결). 나머지 6개 무보정 호출부
(`classify073.py:183`·`taxo_detect.py:118`·`vlm_verify.py:192`·`map_lane.py:161`·
`selection.py:35`·`folder_selection.py:47,139`)는 이번 범위 밖 — Track1/Track2 직접
대조가 가능한 candidates.py만 우선 처리.

---

## [2026-09-08] NVIDIA 데이터셋 전체 workflow 최초 실행 — 오류 점검 결과
> **Task**: multi

**배경**: "전체 workflow를 실제로 진행하며 ①agent 설계상 중복 오류 ②설계 파이프라인상
오류 ③단계별 오류를 점검"하라는 요청. 실행 전 조사에서 **Track1(`tag_v08.tag_clip_v08`)과
Track2(`candidates.generate_candidates`→`retrieve.index_clip`)를 실제로 호출하는 러너가
저장소에 없다**는 사실이 먼저 드러났다(`git log --all -S`로 재확인 — 과거에도 없었음).
이 엔트리는 그 러너(`task_episode/run_workflow_audit.py`)를 신설해 처음 실행한 결과다.

### ① agent 설계상 중복 오류 — `.claude/agents/`(Claude Code 서브에이전트 번들)

외부에서 만들어져 오늘 이 저장소에 복사된 번들(`docs/design/AGENT_DESIGN.md` + 서브에이전트
7개 + pre-commit 인프라)에서 발견·정정한 것:
- `rule-engieer.md`(오타) → `rule-engineer.md`로 리네임(매니페스트와 실물 파일명 불일치 해소)
- `.claude/_common.py` 0바이트 방치 파일 제거(확인 시점엔 이미 병렬 프로세스가 삭제해둔 상태)
- `hooks/hook_utils.py`의 `LINT` 경로 대소문자 수정(`vocab_lint.py`→`Vocab_lint.py`, 실재
  확인 완료)
- **pre-commit 훅 6개 중 4개가 이 저장소에서 상시 무력화돼 있었다** — `VERIFY_DIRS=
  ["verification/"]`·`GOLD_DIR="data/gold/"`·`CARDS_DIR`/`RESULTS_DIR="experiments/..."`가
  가리키는 디렉토리가 전부 미실재. `check_import_separation`·`check_experiment_card`는
  검사 대상이 항상 0건이라 "Passed"가 사실은 "검사할 게 없어서 통과"였다.
  `check_gold_isolation`은 `data/gold/`를 찾는데 실제 관행은 `gold.json`+`gold_label/`라
  실제 gold 참조를 하나도 못 잡는 상태였다. → `GOLD_DIR`을 실제 경로로 정정하고
  `task_selection/review.py`·`task_episode/gold_tool.py`(정당한 참조)를 화이트리스트
  처리, 나머지 두 훅은 실재 디렉토리가 생기기 전까지 "비활성 상태"를 **명시적으로
  출력**하도록 정정(`verbose: true`까지 추가 — pre-commit이 기본적으로 통과 시 출력을
  숨겨서 정보 메시지가 안 보이는 것도 같이 발견해 고침)
- `MANIFEST.txt`/`SHA256.txt` 재검증 — 표본 5개 해시가 5/5 전부 실제 파일과 불일치했음
  (무결성 검증 용도로 못 쓰는 상태). 21개 파일 전부 재해시해 21/21 일치로 갱신
- `AGENT_DESIGN.md`의 설치 지침 경로 오류(`hooks/.pre-commit-config.yaml`→실제는 루트,
  `hooks/_common.py`→실제는 `hooks/hook_utils.py`) 정정
- (미해결, 기계적 수정 아님) 인수인계 파일 계약(`outputs/episodes/`·`data/gold/`·
  `experiments/cards/` 등)이 이 저장소의 실제 산출물 위치(`gold.json`·`gold_label/`·
  `outputs_v08/tags/`)와 다른 세계를 전제하고 있고, rule-engineer(S0+S1)/vlm-engineer
  (S2+S3) 역할 분리가 전제하는 파일 경계가 `tag_v08.tag_clip_v08()` 한 함수 안에 전부
  융합돼 있어 실제로 안 맞음. CLAUDE.md §4 위임 규칙도 미반영— 이번엔 안 건드림

### ② 설계 파이프라인상 오류 (실행으로 확인)

- **최상위 발견**: Track1·Track2 러너 자체가 존재하지 않았다(위 배경 참조) — "전체
  workflow"가 이번이 최초 실행
- **Track1/Track2 arc 불일치 실측 확인**: 50개 중 1개 clip(`024415a5-...`)에서
  Track1 arc=`[accelerate, decelerate, lane_change_right, stop]`, Track2 arc=
  `[accelerate, decelerate, stop]` — **lane_change_right 하나가 Track2에서만 빠졌다.**
  원인은 설계상 이미 알려진 배선 차이: Track1(`tag_v08.py:223`)은 `curvature_fn`을 주지만
  Track2(`candidates.py:111`)는 여전히 무보정으로 `detect_events`를 호출한다(2026-08-28
  배선 이후에도 안 고쳐진 7개 무보정 호출부 중 하나) — **가설이 아니라 실측으로 확인된
  설계 파이프라인 오류**
- **map_valid 20/50(40%)** — Stage1이 실제로 선별한 50개 기준. 기존 실측(28~35%,
  무작위 표본)보다 다소 높지만 표본 방식이 다르므로(Stage1 랭킹 vs 순수 무작위) 직접
  비교는 부적절 — 참고치로만 기록
- **cause 분포 편중**: `agent 71 · other 3 · road_geometry 2 · signal 1`(전체 77
  records). Stage1이 "반응성"(외부 agent 자극에 대한 ego 반응) 기준으로 선별하므로
  자연스러운 결과 — 다만 이 selection bias가 Track1 산출물 분포에 그대로 전이된다는
  걸 보여준다(Stage1→Stage2 연결이 실제로 생기니 처음 드러난 효과)

### ③ 단계별 오류 (S0~S2, 실측)

- **S0**: 50/50 성공, 0건 실패. clip당 평균 1.54 에피소드, 0에피소드 clip 없음(Stage1이
  이미 반응성 있는 clip만 골라서일 가능성). 평균 0.75초/clip로 매우 빠름
- **S1+S2+S3(Track1)**: 50/50 성공, guided_json 파싱 실패 0건, **CJK 혼입 1건/77
  records(1.3%)** — 기존에 확정한 원인(`maxLength` 포화)과 발생률 자릿수가 일치.
  평균 16.1초/clip
- **S1+S2(Track2)**: 50/50 성공, 예외 0건. 평균 22.9초/clip
- 총 소요(순차 기준): 약 33분/50clip. 병렬화 여지 있음(Track1/Track2를 clip별로
  동시에 돌리지 않았음 — 이번 스크립트는 순차 실행)
- **S3(원인 귀속) 미해결 사례 0건** — cause가 전부 확정값으로 나옴(위 분포 참조).
  물리 게이트(준거 B, 목표설계)는 아직 코드가 없어 "미판정" 상태 자체를 산출할 수 없음
  — 이번 실행은 그 게이트 부재 상태에서 나온 결과라는 점을 표기해둔다

**산출물**: `task_episode/run_workflow_audit.py`(신설), `gold_label/workflow_audit/
report.json`(50 clip 전수), `gold_label/select/{select300.json, selected50.json,
index.html}`(Stage1 실행 결과, 최초로 실제 산출).

**영향**: 코드 변경은 `.claude/agents/`·`hooks/`·`docs/design/AGENT_DESIGN.md`·
`MANIFEST.txt`·`SHA256.txt`·`.pre-commit-config.yaml`(전부 미커밋, 사용자 지시에 따라
적용만 하고 커밋 안 함) + `task_episode/run_workflow_audit.py`(신규, 커밋 여부 미결).

**미결(다음 조치 대상)**:
- Track2(`candidates.py:111` 등 7곳)에 곡률보정·차선교차 배선 확대 여부 결정 — 이번
  실측으로 실제 영향(1/50)이 확인됐으므로 §4 전환 규칙(1 브랜치 1 변경축) 따라 별도
  브랜치로 진행
- Track1/Track2를 clip 단위로 병렬화해 33분→단축
- 인수인계 파일 계약과 rule/vlm 역할 분리를 이 저장소 실제 구조에 맞게 재설계(agent 설계
  ①의 근본 미해결 항목)

---

## [2026-09-08] ego_action 함정 해결 — 2축 분리 + Track2 arc 흡수 + map 차선교차 계측
> **Task**: multi

**배경**: S0 검증 리뷰 중 발견된 함정 — `classify073._dominant(kinds)`가 ego_action을
고정 우선순위(stop>evade>turn_left>turn_right>그외=ego_lane_keep) 하나로만 정해서
accelerate/decelerate/lane_change_left/lane_change_right가 arc에는 남아도 ego_action
필드엔 안 드러났다. 조사 결과 `tag_vocab_v0.4.json`의 `ego_action`은 이미
longitudinal(가감속/정지)+lateral(회전/차선변경) 2축으로 정의돼 있었고(목표설계 §2.3
anchor_policy), 현재 코드는 이를 구현하지 않은 v0.7.1 계보 잔재였다. Track2 검색 경로
(`candidates.py`→`retrieve.py`)도 auto_tags_from_arc(u_turn/turn_left/turn_right/stop만)
+ 수동 lane_change만 흡수해 accelerate/decelerate/evade가 검색 태그에서 누락돼 있었다
(`tag_v08._search_tags()`는 arc 전체를 흡수하지만 다운스트림 소비자가 없는 죽은 코드).

**변경**(앵커 비오염 원칙 — 기존 판정 로직은 안 바꾸고 필드만 추가):
1. `task_episode/classify073.py` — `consolidate_episodes()`에 `axes` 필드 신설
   (`_axes()` 함수). longitudinal은 kind 시퀀스 첫/마지막 값으로 transition/
   resulting_state 근사, lateral은 트리거 후보만 담고 `resolved:false`로 "map∪VLM
   해소 전" 상태 명시. 기존 `ego_action`/`dom_kind`는 하위호환으로 그대로 유지.
2. `task_episode/candidates.py` — `ego_cats` 구성을 `auto_tags_from_arc` 결과와
   `ep["kinds"]` 전체의 합집합으로 확장. accelerate/decelerate/evade도 Track2
   candidate·검색 태그에 잡히게 됨(recall-우선 설계와 일치).
3. `task_episode/map_lane.py` — `lane_crossing_count()`/`default_lane_crossing_fn()`
   신설. `common/events.py`의 `detect_events()`에 `lane_crossing_fn=None` opt-in
   파라미터 추가(curvature_fn과 동일 패턴, 미지정 시 기존 동작 완전 동일) — turn/
   lane_change 이벤트에 `lane_crossings` 필드로 **계측값만 기록**, 분류 임계값(45°)은
   안 건드림.

**실측(50-clip S0 표본 재실행)**: `axes` 필드 69/69 episode에 반영. `lane_crossings`는
map_valid clip(14/50)에서 turn/lane_change 이벤트 4건에 값이 붙음. **주의: 값이
노이즈가 크다** — 단일 lane_change_right(2.4초)에 3회, turn_left(5초)에 15회로
비현실적인 수치가 나왔다. 프레임별 경계선 개수를 그대로 세는 1차 구현이라 경계선
검출 흔들림(occlusion·짧은 폴리라인)에 취약한 것으로 추정 — **그래서 계획대로 판정에는
아직 반영하지 않는다.** 정제(스무딩/최소지속 필터)는 후속 작업.

**범위 밖(명시)**: lane_crossings로 실제 turn/lane_change 판정 전환(별도 브랜치 대상),
u_turn을 lateral enum에 추가(원리적으로 ego 기동만으론 불가 — 같은 방향 연속 개별 회전과
진짜 u-turn이 net heading 크기만으론 구분 안 됨, map 위상 정보 필요 — turn류와 동일하게
CAN 트리거+map∪VLM 해소 구조가 필요하나 별도 작업), `_search_tags()`를 실제 검색
기능으로 연결(Track1엔 소비자 자체가 없음), longitudinal resulting_state의 엄밀한
계산(원시 속도/가속도 배열 필요 — 물리정보 가공수준 DV 자체가 미정), `vocab073.EGO_ACTIONS`
flat enum·`_ground_ego_action`은 하위호환 위해 그대로 유지.

**검증**: `py_compile` 통과, `extract_s0_validation.py` 재실행 50/50 성공(seed=42 동일
표본), `gold_tool.py` 무인자 실행 회귀 없음(기존 Track1 워크플로우 영향 없음).

**영향**: `common/events.py`·`task_episode/classify073.py`·`task_episode/candidates.py`·
`task_episode/map_lane.py`·`task_episode/extract_s0_validation.py`.

---

## [2026-09-08] S0 단계 검증 clip 50개 추출 — 단계별 gold 검증의 첫 표본
> **Task**: multi

**배경**: 메타데이터 라벨링 파이프라인을 목표설계 v0.4의 4단계(S0/S1/S2/S3)별로 검증하는
작업의 첫 단계. 지침서 §4.3의 준거A(gold 채점)는 S1/S2/S3만 채점 대상으로 명시하고 S0을
빠뜨렸으나, 같은 절이 `ERR-ANCHOR`("transition 검출 오류·점진 거동 미검출")를 S0 전용
오류코드로 이미 정의해뒀다. 실제 S0 검증 실적은 2026-08-28 엔트리의 37-clip 곡률보정
on/off 대조(gold 없는 회귀 확인) 1건뿐이었다.

**방법**:
- `task_episode/extract_s0_validation.py` 신설. `task_selection/review.sample_pool(50,
  seed=42)`로 기존 gold.json 50개를 제외한 순수 무작위 50개 clip 추출(순환성 배제 —
  임계값 튜닝에 이미 쓰였을 수 있는 clip으로 검증하지 않음)
- 각 clip에 production Track1이 실제 쓰는 배선을 그대로 재현: `map_lane.default_curvature_fn`
  로 곡률보정 여부를 결정한 뒤 `events.detect_events` + `classify073.consolidate_episodes`
  (`tag_v08.py:223`과 동일 호출 방식)
- 곡률보정 미적용 버전도 같이 계산해 2026-08-28의 on/off 대조를 이 표본으로 확장
- `task_episode/gold_tool.py`에 선택적 위치 인자 1개 추가(기본값은 기존 `gold_label/` 그대로
  — 무인자 호출 회귀 확인 완료). `./run.sh task_episode/gold_tool.py gold_label/s0_validation`
  로 기존 review UI(에피소드별 win/arc, 수동 구간 추가로 누락 전이 기록)를 그대로 재사용

**결과**: 50/50 clip 처리 성공(실패 0건). `map_valid`(곡률보정 적용 가능) 14/50(28%).
곡률보정 유무로 kind 분류가 달라진 clip 1건(2%) — 2026-08-28의 1/37(2.7%)과 같은 자릿수로
방향 일치. `gold.json` 기존 50개와 교집합 0건 확인.

**산출물**: `gold_label/s0_validation/{sample_clips.json, episodes.json, s0_raw.json,
vids/*.mp4, index.html}`. `s0_raw.json`은 곡률보정 전/후 원본 이벤트(있는 경우
`curvature_correction_source`만 이벤트 단위로 보존)를 그대로 남겨 감사용으로 쓴다.

**남겨둔 한계 (범위 밖, 명시적으로 미착수)**:
- 실제 사람 리뷰(50개 전수 확인, recall/precision 산출)는 이번 작업 범위 밖 — 리뷰 가능한
  상태까지만 준비
- `transition_filters_passed`·`merged_from`·`window_start_reason`/`window_end_reason`·
  `threshold_set_id`는 여전히 스키마 선언뿐이고 코드 계산 로직이 없다. 이번 추출에서도
  새로 만들지 않았다(범위 확대 방지)
- 지침서 §2.3.1이 요구하는 "필터 통과 직전 탈락 경계 사례" 로그는 `detect_events()`가 중간
  후보를 노출하지 않아 이번 스크립트로 커버 불가 — 필요해지면 `events.py` 자체 수정 필요
- 지침서 §4의 준거A가 S0을 단계별 채점 대상에서 빠뜨린 공백은 여전히 남아있음(이번 표본은
  그 공백을 메우는 재료일 뿐, 채점 기준 자체를 정의하지는 않았다)

**영향**: `task_episode/extract_s0_validation.py`(신규), `task_episode/gold_tool.py`
(하위호환 파라미터화). `gold_label/s0_validation/`는 신규 검증 라운드용 디렉토리로 기존
`gold_label/` Track1 워크플로우와 분리.

---

## [2026-08-31] same-anchor 재현성 실험 — 5-vote는 seed로 해소, tag_v08 잔차는 서빙층
> **Task**: multi

**배경**: 직전 [2026-08-31] 항목("재현성 불일치 조사")의 미결 최우선 항목 — "코드 감사만으로는
검증이 아니다. anchor를 고정하고 실제로 N회 반복 decode해 확인하라" — 을 실행한 결과다.
스크립트 `tmp/repro_experiment.py`(gitignore 대상, 재실행 가능), clip
`12c4c627-1311-42a2-a28c-307e55996e6f`(에피소드 1개, arc
`stop→accelerate→turn_right→decelerate`), 결과는 `tmp/repro_results/full_candidates.json`·
`full_tagv08.json`. 실행: `./run.sh tmp/repro_experiment.py --clip <id> --pipeline both --n 10`.

**방법**: anchor(subclip+프롬프트 텍스트 또는 GT 힌트)를 clip마다 **2회 독립 구성**해 md5/해시
비교로 먼저 고정을 확인한 뒤, 5개 조건 셀(현행/seed고정/포트고정/`n=5`단일요청/포트+seed)마다
같은 anchor로 10회 반복 decode. 두 실험 모두 **anchor는 2회 독립 구성 모두 완전 동일**
(subclip md5·프롬프트 해시·GT 힌트 해시 일치) — anchor 불일치가 원인일 가능성은 이번 clip에
한해 배제됨.

**결과 1 — candidates.py 5-vote(`temperature=0.7`), 10회 중 vote_fraction 패턴이 몇 종류
나왔는지**:

| 셀 | 조건 | distinct 패턴/10 |
|---|---|---|
| a_baseline | 현행 그대로(라운드로빈, seed 없음, `max_retries=2`) | **10/10** (한 번도 안 겹침) |
| b_seed | 라운드로빈 유지 + 투표별 `seed=1000+j` | 3/10 |
| c_pinned | 포트 8001 고정 + `max_retries=0`, seed 없음 | **10/10** |
| d_n5 | 단일 요청 `n=5`, seed 없음 | **10/10** |
| e_pinned_seed | 포트 8001 고정 + `max_retries=0` + `seed=1000+j` | **1/10 — 10회 전부 완전 동일** |

**결론 1**: "5-vote 비결정성"은 실재하며 anchor 문제가 아니다. **포트 고정만으로는(c) 전혀
개선되지 않고, 배치를 단일요청으로 묶어도(d) 개선되지 않는다 — 즉 서빙층 라우팅/배치 자체는
이 현상의 원인이 아니다.** 원인은 거의 전적으로 **seed 부재**(temp=0.7 샘플링에 RNG 고정이
없음)였다. seed만 줘도(b) 10→3으로 급감하고, **포트 고정과 seed를 같이 주면(e) 완전
결정론**이 된다. `candidates.py:138-140`에 `seed=BASE_SEED+j`를 추가하는 것이 검증된
해결책이다(미결 5번 "L1 seed 전달"이 이 실측으로 확정됨).

**결과 2 — tag_v08.py 2-pass(`_structure`, `temperature=0`, guided_json), 10회 반복 시
필드별 안정성**:

- `cause`: **4개 셀 × 10회 = 40/40 전부 `agent`로 완전 안정.**
- `ego_action`: 대부분 `ego_turn_right`이나 **셀·조건과 무관하게 10회 중 0~2회씩 다른 값으로
  튐**(`ego_lane_change_right`/`ego_lane_keep`/`ego_follow`) — `a_baseline` 1회,
  `c_pinned` 1회, `e_pinned_seed` 2회, `b_seed`만 0회(표본 10 규모라 우연일 수 있음).
  포트를 고정하고 seed까지 줘도(e) 사라지지 않았다.
- `scene_description`/`chain_of_causation` 길이는 매 회 제각각(263~400자).
- 부수: pass1(`_reason`, 자유서술) 자체도 완전히 결정적이진 않음 — 같은 anchor로 2회 독립
  호출 시 한 번은 길이가 1808자 vs 1274자로 크게 갈렸고(파일럿 n=1), 본 실행(전체 n=10 세트)
  에서는 2회 모두 1274자로 일치 — **재현 여부 자체가 비결정적**이라는 뜻.

**결론 2**: `temperature=0`(greedy)에서도 이 서빙 스택은 bitwise 재현을 보장하지 않는다.
`cause`처럼 결정 여유가 큰 필드는 흔들리지 않지만, `ego_action`처럼 후보 간 확률차가 근소한
필드는 anchor·포트·seed를 전부 고정해도 흔들린다. seed는 `temperature=0`에서 이론상
샘플링에 관여하지 않으므로(=argmax) 이 잔차는 **seed로 해소되는 종류가 아니다** — 서빙층
(프리픽스 캐시·chunked prefill·cudagraph 배치)의 부동소수 합산 순서 변동이라는 §8.3
`:725`의 원 서술이 이 지점에서 그대로 실측 확인된 것으로 판단한다. 다만 배치 변동을
0으로 만든 조건(요청을 단독으로 서버에 보내 동시 트래픽 0)까지는 이번 실험에서 통제하지
않았으므로, 이 잔차가 "서빙층 자체의 하한"인지 "동시 트래픽에 의한 배치 변동"인지는
추가 실험 대상으로 남긴다.

**갱신되는 미결 항목**: 직전 [2026-08-31] 항목의 "L1 seed 전달"은 본 실측으로 **효과
확정**(코드 반영은 별도 커밋 대상, 이번 세션은 관찰만). "L2는 제거 불가"는 유지하되,
tag_v08 쪽은 seed로 해소되지 않는 잔차가 실측됐다는 점을 구체화함.

**영향**: 코드 변경 없음(`tmp/repro_experiment.py` 신설은 실험 스크립트, `tmp/`는 gitignore
대상이라 저장소 추적 파일에는 영향 없음). `candidates.py:138-140`의 seed 추가가 다음
커밋의 구체적 변경 대상.

---

## [2026-08-31] 재현성 불일치 조사 — 원인은 decode 단이 아니라 집계/순회 층
> **Task**: multi

**배경**: "decode 단의 재현성 불일치를 알고 있어서 free scene description이 아닌 vocab 지정
(닫힌 어휘 점수화)으로 풀려 했다"는 전제로 시작한 조사. 그 전제의 유일한 근거는
`docs/design/pipeline_design_guide_v0.4.md:725`의 "기존에 확인된 5-vote 비결정성" 한 줄이다.
코드 전수 확인 + 서빙 컨테이너 실측으로 이 전제를 검증했다.

**관측**:

0. **방법론 한계 — "같은 입력으로 비교했는가"를 이 조사도 직접 검증하지 못했다.**
   이 조사는 plan mode에서 3개 탐색 서브에이전트를 배경 실행해 코드·문서를 감사하는 방식으로
   진행됐다 — 특정 clip에 대해 실제로 두 번 decode를 돌려 anchor(프롬프트 텍스트·서브클립)를
   로그로 남기고 diff한 **통제 실험이 아니다.** 사용자가 "정확히 같은 입력으로 비교한 게
   맞는지" 확인을 요청해 아래 1번을 재검증했고, 그 결과 **원인으로 지목한 버그가 실제 decode
   비교 경로에는 닿지 않는다는 것이 드러났다** — 최초 서술(아래)은 과잉 일반화였다.
   근본 문제: `pipeline_design_guide_v0.4.md:725`의 "기존에 확인된 5-vote 비결정성"도 언제
   누가 무엇을 비교해 확인했는지 기록이 없어(6번 참조) 이 조사가 그 원 관측을 재현·반증할
   방법이 없다. **아래 1은 "무엇이 비결정적인가"에 대한 정확한 관측이지, "5-vote decode
   비교가 이것 때문에 무효였다"는 검증은 아니다.**

1. **확정(실측)이나 활성 decode 경로에는 닿지 않음 — L3 집계/순회 층은 VLM 없이도
   산출물을 바꾸지만, 범위는 dead code·legacy에 한정된다.**
   `common/events.py:273` `for k in set(tid.tolist())` — parquet의 `track_id`가 문자열이고
   `run.sh`에 `PYTHONHASHSEED` 고정이 없어, 동일 입력을 3개 프로세스에서 실행하니 순회 순서가
   매번 달랐다(`80 ['120','156','97',...]` / `['142','21','77',...]` / `['29','21','30',...]`).
   이 순서가 `:307-313`의 안정정렬+dedup에서 "어느 트랙이 남는지"를 결정하고(`min_dist`가
   `:288`에서 `round(...,1)`로 양자화되어 동점이 잦음), 결과가 **`common/events.py:251`
   `detect_obstacle_events()`를 거쳐 `classify073.tag_clip_v073`·`legacy/event_tagger.py:130`
   으로만 흐른다.**
   재확인 결과 `tag_clip_v073`을 호출하는 활성 코드가 **없다**(`tag_v08.py`/`candidates.py`/
   `vlm_verify.py`/`folder_selection.py`는 `classify073`에서 `consolidate_episodes`·
   `_causal_agent` 등만 가져다 쓰고 `tag_clip_v073`은 아무도 호출하지 않음). 실제 decode
   anchor를 만드는 활성 경로는 별개 함수를 쓴다: `tag_v08.py:226`의
   `detect_obj3d_events()`(`events.py:187-248`)는 `tracks.items()`(**dict**, 삽입순 —
   해시 랜덤화 영향 없음)를 쓰고, `taxo_detect._load_tracks`(`:66`)는 `range(len(...))`로
   파일 순서 그대로 순회한다. `candidates.py`/`vlm_verify.py`가 프롬프트에 넣는 GT 힌트도
   `vlm_verify.py:105` `sorted(cands)`로 정렬 후 삽입된다. 서브클립 영상도 md5 3회 동일(4번).
   **즉 활성 파이프라인(tag_v08/candidates)의 anchor(GT 힌트 텍스트 + 영상)는 이번에 재확인한
   범위에서 실행 간 동일하다** — 이 버그로는 그 경로의 decode 비교 불일치를 설명할 수 없다.
   집계/순회 층에 남아 있는 활성 경로 결함은 **anchor 내용이 아니라 산출물 순서/선별**에
   국한된다: `folder_selection.py:277-285`(무이벤트 클립 `clip_score=0.0` 대량 동점 +
   `as_completed` 수집 순서 → **어느 clip이 top-K에 뽑히는지**가 실행마다 달라짐 — 이건
   "같은 clip의 decode 비교"가 아니라 "애초에 어느 clip을 도느냐"의 문제), `:247`(어느 3개
   윈도우를 VLM에 보낼지가 동점으로 갈림 — 이 역시 anchor 선택 단계, decode 자체 아님),
   `candidates.py:125,131,143-151` set 순회 → `retrieve.py:67,75,81` **출력 배열 순서**(값 아님),
   `sort_keys=True` repo 전체 0건(§8.3 `:731` 해시 비교가 키 순서만으로 실패),
   `folder_selection.py:279-283` 표본 상대 정규화, `review.py:26,35`(seed=42 고정이나 pool이
   `os.listdir` 산물이라 클립 1개 증감에 표본 300개 재배치 — `:33` 주석은 풀 불변일 때만 참).

2. **유력·미계측 — L2 서빙 층.** `cosmos_dj`(8001) 컨테이너 env 실측:
   `NIM_ENABLE_KV_CACHE_REUSE=1`(프리픽스 캐시 ON), `NIM_ENABLE_CHUNKED_PREFILL=1`,
   `NIM_MAX_NUM_SEQS=256`, `cudagraph_mode=FULL_AND_PIECEWISE`, seed 관련 env 없음.
   §8.3 `:725`가 원인 후보로 지목한 경로가 전부 활성 — `temperature=0`이어도 bitwise 재현이
   보장되지 않는다. 클라이언트도 부하 변동을 만든다: `ThreadPoolExecutor(8/12)`
   (`folder_selection.py:274,300`), 5표를 서로 다른 복제본으로 분산(`candidates.py:139`),
   `common/client.py:16-23` 헬스체크(2s) 통과분만 풀 구성 → `len(pool)` 1~4 가변,
   openai SDK 기본 `max_retries=2` 무기록 재시도.

3. **반증 해소 — 복제본 가중치 동일성.** `trunk_실측_20260814.md:97`의 "8001·8002-4가 다른
   이미지 → 가중치 동일성 미검증" 지적을 재확인: `cosmos_dj_snap:latest`(f8dcbf66)의 Parent가
   `nvcr.io/nim/nvidia/cosmos3-reasoner:latest`(19add3ed)이고, 4개 컨테이너 env 해시 동일,
   `/v1/models`의 `created` 4개 모두 `1788134979`. snap은 베이스 이미지 위에 NIM 캐시 레이어를
   commit한 자식 — 별개 가중치 아님.

4. **용의선상 제외 — 프레임 추출.** `sample_montage`/`write_subclip` 각 3회 실행 md5 동일
   (5,727,505B/181frames). OpenCV 시크·mp4v 인코딩은 이 빌드에서 재현적.

5. **설계상 의도 — L1 5-vote.** `candidates.py:26-27` `VOTE_N=5`,`VOTE_TEMP=0.7` — 저장소 유일의
   temp>0 경로. 호출 14곳 전부 `seed`·`top_p`·`top_k`·`n`·`logprobs` 미전달이고, `client.py`가
   파라미터를 캡슐화하지 않는 순수 팩토리라 공통 주입 지점 자체가 없다. → "불일치"가 아니라
   **재현 불가**. seed 전달로 대부분 해소 가능(미착수).

6. **근거 추적 불가.** §8.3 `:725`의 "기존에 확인된 5-vote 비결정성"은 DESIGN_LOG·logs/(25개)·
   docs/ 어디에도 원 관측(날짜·수치·해시 비교)이 없다. `tag_vocab_v0.4.json`의
   `versioning.reproducibility_params` 5군을 기록하는 코드도 없다(`disclosure.py`의 4필드가
   유일한 메타이며 디코딩 축 없음). `CLAUDE.md:88`이 정본으로 지목한
   `실험계획_추론경로_260824.md`는 main 워킹트리에 없다(실체는 워크트리의
   `docs/experiments/experiment_design_v0.1.md`). **[2026-09-11 갱신] 문서 소재는 확인·
   반영됨(맨 위 `[2026-09-11] 실험계획_추론경로_260824.md 실체 확인` 항목 참고). 다만 그
   문서 자체가 이 6번이 찾는 "5-vote 비결정성의 원 관측(날짜·수치·해시 비교)"을 담고
   있지는 않으므로, 이 6번의 본체(원 관측 부재)는 여전히 미해소다.**

**판단**: vocab 지정(닫힌 어휘 점수화) 전환을 **재현성 근거로 추진하지 않는다.** 1번 재검증
결과 활성 파이프라인의 anchor는 이미 결정적이므로, 그 경로에서 진짜 decode 비결정성이
관측된다면 원인은 "anchor가 달랐다"가 아니라 2번(서빙층: 프리픽스 캐시·chunked prefill·
cudagraph·배치 변동) 또는 5번(5-vote는 설계상 temp=0.7+seed 없음)일 가능성이 높다. 워크트리
프로토타입 `task_episode/seq_score.py`(`.claude/worktrees/selection-dist/`)도 `logprobs=True,
top_logprobs=K`로 동일한 L2 서빙 경로를 탄다 — vocab 지정이 서빙층 비결정성을 우회하지
못한다. 점수화는 후보 간 logprob 근소차에 argmax가 걸리는데, 실측된 충돌 필드 9개
(`tag_vocab_v0.4.json` `scoring_method.collision_note`, 2026-08-28: 19개 중 9개 — 좌/우 쌍 +
`road_geometry`/`road_condition`,`stop_sign`/`stop_line` 등 접두어 공유 계열)가 정확히 그
지점이다. 전환은 증상을 "텍스트가 눈에 띄게 다름"에서 "점수 1e-3 차이로 라벨이 조용히
뒤집힘"으로 바꿀 뿐이다. vocab 지정의 정당한 근거는 재현성이 아니라 어휘 곱 폭발 억제·판정
일관성(지침서 §1.4, `:81`)이다.

**부수 발견(수정은 별도)**: `common/dataset.py:269,274`가 원본 30fps 프레임을 전부 쓰면서
컨테이너 fps 헤더만 `SEND_FPS=10`으로 선언 — 실측 6.0s 창이 181프레임/선언 18.1초(3배 팽창).
서버는 `NIM_MEDIA_IO_KWARGS={"video":{"fps":4.0}}`로 **선언 타임라인 기준** 재샘플하므로,
CLAUDE.md의 "서버 ~12프레임 캡" 서술은 캡이 아니라 4fps 재샘플이며 §4 전환 규칙("캡이 상한")의
전제가 부정확하다.

**미결(다음 조치 대상, 이번 세션에서 착수 안 함)**:
- **통제된 same-anchor 재현성 실험 (최우선, 0번의 대안)** — 이번 조사는 코드 감사이지 실측
  실험이 아니다. 실제 검증은: 특정 clip·윈도우 1개를 고정 → anchor(프롬프트 텍스트 전문·
  서브클립 md5·GT 힌트)를 로그로 저장 → `tag_v08._structure`/`candidates._vlm_present`를
  N회 반복 호출 → anchor 로그가 N회 모두 동일한지 먼저 확인하고, 동일함에도 decode 출력이
  갈리면 그때 L2(서빙층)로 원인을 좁힌다. 이 실험 없이는 "5-vote 비결정성"도 이번 조사의
  1번 정정도 최종 확정이 아니다
- L3 결정화: `PYTHONHASHSEED=0` 고정, `events.py:273`류 set 순회 `sorted()`화, 정렬 2차 키 추가,
  산출 JSON `sort_keys=True`
- L1 seed 전달: `common/client.py` 공통 호출 래퍼 신설 후 `candidates.py` n-vote에 `seed` 부여
- L2는 제거 불가 — 배치/캐시 민감도를 계측해 허용 오차로 문서화(§8.3 `:732`)하고 평가 실행
  규약을 단일 엔드포인트+`workers=1`로 고정하는 방안 검토
- L4 매니페스트: `common/manifest.py` 신설, `tag_vocab_v0.4.json`의 재현성 5군 스탬프
- CLAUDE.md의 "서버 ~12프레임 캡" 서술 정정

**영향**: 코드 변경 없음(조사·기록 전용). `common/events.py`·`task_episode/candidates.py`·
`task_selection/folder_selection.py`·`common/client.py`·`common/dataset.py`가 향후 수정 대상.

---

## [2026-08-28] 설계변수 기본값 코드 구현 1차 — 곡률보정 Stage2 배선, GT배치·공개필드 배선
> **Task**: multi

- 변경:
  1. **B1 결정**: `common/gt_placement.py` 신설 — GT 주입(DV-8) 미분류 필드 기본값을 `gt_before`로 확정(기존 `gt_free` 안이 아님). 이유: `gt_free`(미주입)는 grounding 실패 위험을 조용히 방치하지만 `gt_before`(관측 사실 주입)는 최악의 경우도 과잉 주입일 뿐 정보 손실이 아님 — 안전한 쪽 강등.
  2. **공개의무 배선**: `common/disclosure.py` 신설, `tag_v08.py`/`candidates.py`/`vlm_verify.py`에 `flags`로 스탬프, `retrieve.py`에 pass-through. `s2_conditioning` 실측값(`rule_injected`)이 스키마 선언 기본값(`minimal`)과 불일치함을 그대로 기록 — 코드 동작은 안 바꾸고 사실만 정직하게 남김(§3 GT 힌트 전환 규칙 준수).
  3. **곡률보정(A2) Stage2 배선**: `map_lane.road_curvature_over`/`default_curvature_fn` 신설, `events.detect_events(curvature_fn=...)` opt-in 파라미터 추가(기본 None — 미지정 호출은 전부 기존 동작 그대로, map import 없음), `tag_v08.py`에 기본 배선.
  4. `decisions/DV_DEFAULTS.md` 신설 — 설계변수 전체의 현재 기본값·실행상태 스냅샷(살아있는 레퍼런스, 결정 이력 아님).

- 근거(곡률보정 안전성 실측): 데모 150 clip 중 map_valid=True 37개 전체에 on/off 대조 실행(VLM 불필요, `detect_events`+`consolidate_episodes`만). **kind 분류가 바뀐 clip 1개(2.7%), 에러 0건.** 그 1건(`d2f9f633-...`)은 raw heading 49°(문턱 45° 바로 위) · lat 7.4m(차선변경 대역 밖) · map frac 0.9(고신뢰) 조건에서 turn_left가 완전히 소거됨 — 설계 의도(완만한 대곡선의 회전 오검출 방지)와 정확히 일치하는 방향. 경계값+map_valid 동시 조건 실측 케이스는 이 1건뿐이라 폭넓은 검증은 아니지만, 낮은 blast radius(37개 중 1개)와 방향 일치성을 근거로 배선 확정.

- **자체 회귀 발견 및 정정**: 이번 작업 중 `common/vocab073.py`가 모듈 최상단에서 `meta_tagging_*_v0.7.3.json` 3개를 무조건 로드하는데, `tag_v08.py`가 `from vocab073 import EGO_ACTIONS, OBJECT_TYPES, RELATIONS`로 **직접**(non-lazy) import한다는 걸 이전 v0.7.3 스키마 삭제 결정(2026-08-28 앞선 엔트리, 본 로그 미기재분) 당시 놓쳤다. 그때는 `classify073.tag_clip_v073()`(비활성 함수)만 확인하고 `tag_v08.py`의 별도 top-level 의존을 못 봤음 — Track1 authoritative 경로 import 자체가 깨져 있었다. `git checkout HEAD --`로 4개 파일 전부 복구(main+워크트리), import 정상화 재확인.

- 영향: `common/gt_placement.py`·`common/disclosure.py`·`task_episode/map_lane.py`·`common/events.py`·`task_episode/tag_v08.py`·`task_episode/candidates.py`·`task_episode/vlm_verify.py`·`task_episode/retrieve.py`. Stage1(`folder_selection.vlm_curve_check`/`verify_ambiguous_curves`)은 구현만 하고 랭킹 경로엔 미배선(VLM 비용 검토 후 별도 진행).

---
> **Task**: episode

# DESIGN_LOG — 2026-08-24 설계 지침서 v0.4 도입

기존 항목 아래에 이어 붙인다. 각 엔트리는 **무엇을 / 왜 / 무엇이 바뀌는가**로 구성한다.

---

## D-2026-08-24-01 · 설계 지침서 v0.4 및 어휘 v0.4 도입

**변경**: clean-slate 설계서(v0.1)와 구 계열(meta_vocab v0.1)을 병합한 v0.4를 목표 설계 정본으로 채택. 어휘는 `tag_vocab_v0.4.json` 단일 원천.

**근거**: 두 계보를 대조한 결과 각각이 상대의 공백을 메웠다. clean-slate 쪽이 원칙 수준 결함 4건을 잡았고(가시성≠존재, 단계 병렬 구조, Action-first 배제 범위 명시, 중간 산출물 영속화), 구 계열 쪽이 소비자 계층 6건을 보유했다(파생 뷰, 선별 접속, 규칙 산출성과 신뢰성 분리, 표지 계층화, 판정 범위 2단, gold 복수 정답).

**영향**: 코드는 아직 v0.4를 구현하지 않는다. CLAUDE.md를 3층 구조(현재 코드 실태 / 목표 설계 / 불변 규칙 + 전환 규칙)로 재작성하여 두 층의 혼동을 막는다.

---

## D-2026-08-24-02 · 앵커 정의 정정 — 경로 유형 라벨은 CAN 확정값이 아님

**변경**: "자차 거동은 전량 CAN anchored" → **"전이 시점과 종방향은 CAN 규칙 확정, 경로 유형(좌/우회전·차선변경·U턴) 라벨은 지도 ∪ VLM으로 해소"**.

**근거**: CAN 요레이트만으로는 교차로 회전 / 도로 곡률 / 분기 / U턴이 구분되지 않는다(under-determined). 이는 저장소에 이미 기록된 제약이었으나, 어휘 재설계 과정에서 축을 "순수하게" 만들려는 단순화 압력으로 누락되었고 clean-slate 재작성이 복원 기회를 없앴다.

**영향**:
- 경로 유형 라벨은 규칙 확정값이 아니므로 **평가 대상**이며 기계 채점 기준(AUTO_GT)으로 사용 금지
- 앵커 비오염 원칙의 적용 범위는 **전이 시점 확정**까지. 경로 라벨의 맥락 해소는 예외가 아니라 이후 단계
- 지침서 §2.3, 어휘 `ego_action.anchor_policy`, CLAUDE.md §3에 반영

**되돌린 결정**: `vocab_lint.py`의 `RETIRED`에 `"전량 CAN"` 등록

---

## D-2026-08-24-03 · 미래 정보 차단 원칙 폐기

**변경**: "`key_frame_t` 이후 관측을 인과 귀속 근거로 사용 금지" → **폐기**. 판정에는 시간창 전 구간을 제한 없이 사용한다.

**근거**: 본 산출물은 라벨이지 예측기가 아니다. 태깅 시점에 이미 전 구간이 확보되어 있으므로, 사후 정보를 막으면 인과 판정 정확도만 낮아지고 얻는 것이 없다. 원래의 우려(사후 정보 기반 인과 라벨로 예측 모델을 학습하면 추론 시점에 없는 정보를 전제하는 패턴을 배움)는 **소비 단계 주의사항**이지 파이프라인 제약이 아니다.

**영향**:
- 인과 라벨의 판정 근거 범위를 export 문서에 표기 — 예측 모델 학습 용도 시 시점 제한 재라벨 필요 가능
- 위반 금지 원칙 2번을 "판정 근거 시점 표기"로 교체
- 이에 딸려 설계했던 영상 블록 분할(인과용/전체용 프리픽스)도 철회

**되돌린 결정**: `RETIRED`에 `"미래 정보 차단"` 등록

---

## D-2026-08-24-04 · 목적 우선순위 원칙 신설 — 라벨 품질이 측정 편의에 우선

**변경**: 상위 원칙 신설(지침서 §1.3.1). 측정 목적 제약이 라벨 품질을 낮추면 **측정 쪽을 조정**한다(표본 한정 이중 실행, 조건 분리 보고). 전역 금지는 순환성·재현성처럼 어기면 결과가 무효가 되는 항목에 한정.

**근거**: 미래 정보 차단과 같은 계열의 제약이 문서 전반에 흩어져 있었다. 개별 수정만으로는 같은 유형이 다시 유입된다.

**영향 — 함께 완화된 항목 2건**
- **S2 입력의 최소 단서**: 금지 → **기본값**. 규칙 판정 주입이 라벨 정확도를 높이면 채택하고 전파율은 표본 한정 독립 실행으로 측정(실험 C)
- **프레임·뷰 순환 배제**: 기본 샘플링은 후보 비의존 유지, **후보 기반 추가 프레임은 플래그 기록 후 허용**. 뷰는 축소만 금지하고 확대는 허용(축소는 누락을 만들고 확대는 만들지 않음)

**유지된 항목**: 물리 게이트의 시간 순서(인과율 자체), 반응 없는 원인의 에피소드 미생성(Action-first 구조 결정)

---

## D-2026-08-24-05 · S1 물리 정보의 S2 주입 — 3-way 확정

**변경**: 주입 클래스를 `gt_only` / `gt_before` / `gt_free` **3종으로 확정**. 영상 뒤 주입(`gt_after`)은 기본값에서 제외.

**근거**: `gt_after`의 도입 근거였던 "모델이 규칙과 다른 값을 낼 여지를 남겨 모순 탐지"는 **주입 없이도 성립**한다 — VLM 독립 실행 후 규칙 판정과 사후 대조하면 동일 신호를 얻고 토큰 비용이 낮다. 규칙 맥락이 필수인 경우는 객체 참조 성립뿐이며 `gt_before`가 담당.

**영향**:
- 필드 단위 배정표 확정(지침서 §3.1 DV-8) — 판정 기준 3개(정보 없이 답할 수 있는가 / 복사만 하는가 / 편향시키는가)
- `gt_before` 작성 규칙: **관측 사실만**. "감속 중"은 관측, "위험"·"끼어드는 중"은 판정이므로 제외
- 금지가 아니라 기본값 선택 — 실험 C에서 라벨 정확도 이득 확인 시 재상정

**되돌린 결정**: `RETIRED`에 `"gt_before → gt_after"`, `"GT-after 블록"` 등록

---

## D-2026-08-24-06 · 전이 검출 4중 필터 구조 확정

**변경**: 앵커 검출에서 사건이 아닌 변화를 거르는 **규칙 구조**를 고정(임계값은 별도). 순서: 크기 → 지속 → 곡률 보정 → 병합.

**근거**: 도로 곡률 추종 조향·차로 내 흔들림·노면 요철·가다서다가 걸러지지 않으면 에피소드가 폭증하여 모집단 자체가 무의미해진다. 임계값을 하이퍼파라미터로 미루면서 **구조까지 함께 미룬 상태**였다.

**설계 결정 3**:
- 횡방향은 순간 요레이트가 아니라 **누적 방위 변화량** — 완만한 대곡선과 급한 소회전이 순간값으로 구분되지 않음
- 곡률 보정은 자차 방위 변화에서 **도로 기하가 설명하는 몫을 뺀 잔차**로 판정. 보정 출처(`map`/`estimated`/`none`)를 기록
- 병합은 마지막 — 앞 필터 통과분끼리만 병합해야 미미한 변동이 되살아나지 않음

**영향**: `transition_filters_passed`·`curvature_correction_source`·`merged_from` 기록 의무. 필터 통과 직전 탈락 사례를 별도 로그로 축적하여 임계 조정 근거로 사용

---

## D-2026-08-24-07 · 규칙 불확실성 5신호 정의 (참조 유실 복구)

**변경**: `rule_uncertainty` 5신호에 산출 방법 정의. v1 가동 범위는 **지도 유효성 + 기하 여유 2개**로 한정.

**근거**: 지침서 2곳이 참조하는데 어휘에서 `null`이었다. 구 계열(v0.8.1)에서 정의했던 것이 clean-slate 어휘 교체 시 유실되어, **참조는 살아남고 정의만 사라진** 상태였다.

**영향**: 5신호 동시 튜닝은 근거 없는 상수를 5개 늘린다. 나머지 3신호(검출 품질·소스 상충·시계열 흔들림)는 값만 기록해 분포를 축적한 뒤 임계를 도출

---

## D-2026-08-24-08 · 물리 게이트 6검사 판정 로직 정의

**변경**: 항목명뿐이던 B 게이트에 입력·판정 로직·위반 처리 명시.

**핵심 2**:
- **반응 지연은 단일 상수 금지** — 신호 대기와 끼어들기 대응의 지연 분포가 다르므로, 하나의 범위로 묶으면 특정 원인 유형이 체계적으로 탈락한다. 원인 유형별 gold 실측에서 도출
- **위반 시 라벨 삭제 금지** — 위반 항목을 기록해 오류 유형 분류·재라벨 우선순위에 사용. 삭제하면 임계가 잘못돼도 통계에서 사라져 알 수 없다

**영향**: 산출은 `오답 확정` / `미판정` 두 값뿐. **통과율을 정확도로 보고 금지**(위반 금지 원칙 5)

---

## D-2026-08-24-09 · 추론 실행 방식 — 3층 구성 (잠정)

**변경**: 필드 처리 경로를 3층으로 구성. GT 필드는 호출 없음 / 의미 필드는 **생성 1회 + 결과 JSON 재프리필** / 판별이 갈리는 소수 필드만 후보 시퀀스 채점 보정.

**근거**: 필드별 개별 질의(약 35회)와 전체 생성(디코드 300~400토큰) 사이의 균형점. 값이 채워진 JSON을 다시 프리필하면 모든 필드 값 자리가 존재하므로 **한 번의 프리필로 전 필드 분포**를 얻는다.

**주의**:
- 재프리필 확신도는 모델이 스스로 쓴 값에 조건화된 값이다. "정답일 확률"인지 "자기 답에 대한 확신"인지는 gold 대비 calibration으로만 구분
- **점수화 기본값은 시퀀스 방식**. 한 위치 분포만 읽는 방식(first-token)은 후보 첫 토큰이 같으면 판별 불가이며, 실측상 충돌 38개 값의 대부분이 **좌/우 쌍**이라 최상위 실패 모드에서 판별력을 잃는다
- **잠정 구성** — 실험 A(arm ①~⑤)로 확정

---

## D-2026-08-24-10 · 어휘 정합성 검사 도구 도입

**변경**: `vocab_lint.py` 추가. 검사 9종 — 폐기 표현 잔존 / 개념 중복 정의 / enum 명명 혼용 / auto_checks 참조 / 파생 뷰 참조 / first-token 충돌 / 지침서 폐기 표현 / **지침서→어휘 참조 무결성** / gt_only↔rule_derivable 교차.

**근거**: 이번 세션에서 부분 수정 후 잔존물이 남는 사고가 반복되었다(window 정의 3곳, gt_after 2곳, enum 명명 2건). 사람 검토로는 놓친다.

**도입 즉시 검출된 실제 불일치**:
- `window`와 `episode_structure.window_semantics`의 개념 중복 → 최상위 `window` 단일 정본으로 통합
- 지침서 `prev_transition`·`budget_truncated` ↔ 어휘 `prev_ego_transition`·`budget_truncation` → 어휘 쪽으로 통일
- 품질 등급이 `"rule_verified — 설명"` 형태의 결합 문자열 → 키-설명 분리(기계 참조 가능하게)

**운용 규칙**: 어휘·지침서 변경 커밋에서 실행. **결정이 뒤집힐 때마다 `RETIRED` 사전에 항목 추가** — 이 목록이 본 로그의 "되돌린 결정"과 짝을 이룬다

---

## 미결 (다음 결정 대상)

| 항목 | 결정 주체 | 선행 조건 |
|---|---|---|
| 어휘 파편화 정리 (현행 4벌 → 단일) | 설계 회의 | v0.4 도입의 전제 |
| 파생 뷰 원칙 위반 리팩터 범위 | 설계 회의 | 뷰 이름이 검출 로직에 박힌 범위 산정 |
| 임계값 전체 (전이 검출·반응 지연·불확실성·후보 범위) | 실측 후 선언 | gold·분포 실측 |
| 추론 경로 확정 | 실험 A | 서빙 재기동 |
| first-token 적용 가능 필드 | 실험 B | 토크나이저만으로 즉시 가능 |
| GT 주입 배치 재검토 | 실험 C | §2.2 상충 판정 완료(D-04로 해소) |
| 신뢰도의 경로 간 비교 가능성 | 검증 | 실험 A 결과 |


## [2026-08-03] Task「선별」실행파일 배포 패키징 (외부 기관 서버, 코드 비노출)
> **Task**: selection
- 변경: `deploy/` 신규 — ① `app/runtime.py` 외부 설정(JSON/env) → **모듈 네임스페이스 주입**(paths·config·client·selection·folder_selection)으로 원본 소스 수정 없이 데이터경로·VLM엔드포인트 교체 ② `app/cli.py` `rank`/`review`/`selftest`/`print-config` ③ `shield.py` 빌드 스테이징에서 프롬프트·카테고리 상수를 난독 blob으로 AST 치환 ④ `build.sh` 스테이징→shield→Nuitka onefile→검증(strings)→SHA256SUMS ⑤ `Dockerfile` 멀티스테이지(최종 이미지에 소스 부재) ⑥ `app/guard.py` 선택적 유효기간·호스트잠금.
- 이유: 타 기관 서버에 올려 구동하되 알고리즘(점수식·임계값·프롬프트·융합 로직) 노출 방지. 파이썬 배포는 소스/바이트코드가 그대로 노출되므로 네이티브 컴파일이 최소 요건.
- 부수 변경: `task_episode/classify073.py` 의 `vocab073` import를 **지연 로드**(`_v()`)로 전환. import 시점에 `schema/*.json` 3개를 읽던 것을 제거 — Stage1 선별 경로는 `consolidate_episodes` 만 쓰므로, 배포물에 **Stage2 taxonomy 자산이 아예 들어가지 않게** 됨(동작 불변).
- 보호 범위(정직): 소스·바이트코드 부재·docstring 제거·프롬프트 평문 미노출은 확보. 그러나 (a) **VLM 서버를 상대가 호스팅하면 요청 로그로 프롬프트 노출**(구조적) (b) 네이티브 리버싱은 여전히 가능 (c) 입출력 대량 관측으로 랭킹 함수 근사 모방 가능 (d) guard는 계약 이행 보조일 뿐 보안 경계 아님. → 컨테이너+유효기간 빌드+NDA 조합 권고, 프롬프트가 결정적 IP면 추론 엔드포인트를 우리가 운영.
- 산출물 노출 축소: `ranking.json` 에서 점수 기여항(`terms`=내부 가중치)은 기본 제외(`output.include_score_terms` 로만 노출).
- 검증: 소스모드/바이너리 모두 실데이터(1966 clip) selftest·rank·리뷰HTML 통과. VLM 서버 8001–8004 전부 down이라 **VLM 채널은 미검증**(구조적 경로만 확인).

## [2026-08-03] 타 기관 큐레이션 인터페이스 정의서 — 질의=세그먼트 / 반환=클립
> **Task**: multi
- 맥락: 두 작업이 별개다. **(A)** 우리 labeling 파이프라인을 타 기관 서버에 올려 **우리가** 실행(→ 실행파일 wrapping·IP 보호). **(B)** 확보된 metadata로 **타 기관이** 검색엔진 기반 큐레이션 알고리즘을 개발(→ 이 정의서). 상대는 라벨링도, 우리 실행파일도 다루지 않는다.
- 변경: `deploy/schema/` 를 작업별로 분리 — `selection/`(A: 실행파일 산출물 스키마, dist 동봉) · `curation/`(B: `INTERFACE_SPEC.md` + 세그먼트 교환용 사본 + `clip_index` 스키마) · `SHARE.md`(공유등급·검증기록).
- 설계 결정: **질의 단위=세그먼트(의미 축 보유) / 반환 단위=클립**. 세그먼트가 걸리면 소속 클립이 함께 결과에 포함되고, 세그먼트는 근거·구간 위치로 동반. 한 클립 다중 매치는 dedup 후 근거 배열. 이를 위해 `clip_index`(clip_id·duration_s·n_segments·video) 상위 엔티티를 신설 — 세그먼트 0건 클립도 유지.
- 범위: **Track1(세그먼트 메타태깅)만.** Track2 카테고리 후보(카테고리·confidence·provenance)는 recall-우선 후보라 임계값 책임이 상대로 넘어가므로 이번 정의서에서 제외, 카테고리 검색을 열 때 별도 버전.
- 교환용 사본 = 내부 정본에서 `x_generation`(앵커/생성순서)·`x_constrained_decoding`(guided_json 강제) 제거 + `x_record_unit`·`x_field_source` 추가. `properties`·`x_fill_rules`·`x_role_derivation` 은 해석에 필요하므로 유지.
- 비공유: `meta_tagging_opt1_model_output_v0.7.3.json`(무엇을 모델에 생성시키는지 = 설계 IP), `meta_tagging_vocab_v0.7.3.json`(라벨 공간 설계 — 상대가 라벨링을 안 하므로 불필요), 프롬프트, 가중치. 값 의미 사전·검증 절차·참조 구현도 제외(상대 개발 영역).
- 기준: **상호운용에 필요한가(→준다) vs 그 답에 도달한 방법인가(→안 준다)**.
- 검증(실데이터): `outputs_v073/tags/` 실제 산출물 10파일·세그먼트 14건으로 교환본 draft-07 검증 위반 0, `clip_index` 조립·조인 무결성(고아 0, n_segments 일치, 0건 클립 유지) OK, 세그먼트 질의→클립 롤업 동작 확인(cause=agent 5세그→3클립 등).
- **검증 중 발견·수정**: 시간 필드의 `maximum: 20` 제약이 실측 클립 길이(20.01~20.08s)를 탈락시킴 → **내부 정본(`common/schema/meta_tagging_seg_opt1_schema_v0.7.3.json`)과 교환본 양쪽에서 상한 제거**. 기존 태깅 산출물의 `warnings` 에 이미 찍히던 건.

## [2026-07-31] 설계 문서 통합 — PROJECT_DESIGN 단일 정본
> **Task**: multi
- 변경: `docs/Concept_Design_v3` 제거(내용 90% PROJECT_DESIGN와 중복, 고유=수용기준만). `docs/PROJECT_DESIGN.md`를 **단일 정본**으로 재작성 — 개요(2단계·Track1/Track2)·데이터·아키텍처·관점전환·택소노미(5축60키)·Phase 상태표·가드레일·핵심발견·수용기준(흡수)·**현재상태&업무분담(WP1–8)**·코드맵. 참조 갱신(docs/README·CLAUDE.md·TEAM_REPORT·decisions/README).
- 이유: 두 문서 역할 동일(v3 설계)인데 stale(2026-07-23, Track1/Track2·windowed 승격·taxonomy 확장 미반영). 팀 업무 분담 목적이라 최신 상태+작업 패키지 필요.
- 영향: 설계 정본 1개로 단일화. 팀 공유/분담 기준.

## [2026-07-31] Stage1 기본 선별 = 윈도우+video 반응성으로 승격
> **Task**: selection
- 변경: `run_selection.py` 기본 경로를 **윈도우+video 반응성**(`folder_selection.rank_folder_windowed`)으로 전환. 몽타주 흥미도는 `montage` 서브커맨드(legacy)로 강등. `review.py`로 트랜스코드/HTML/샘플링 공용화(중복 제거). canonical 산출(index.html/select300.json)을 windowed 결과로 정본화(기존 winevent 재사용, 재컴퓨트 없이 `review` 재생성).
- 이유: obj3d 대조 검증에서 몽타주 cut_in corroboration 6%(top50 0%) vs 윈도우+video는 과검 대부분 제거·reactive 50/50. 시간정렬+video가 주변부 투영/오귀속을 구조적으로 해소. Stage1 전제(obj3d-free, CAN+VLM)는 유지(윈도우 엔진도 egomotion+VLM video만 사용).
- 영향: `run_selection.py`·`SELECTION_STAGE1.md`·README. 엔진은 `folder_selection`에 상주(Stage1 global + 폴더 공용). montage 흥미도 경로 보존(비교용).

## [2026-07-31] cut_in obj3d 검증 (Stage1 산출 사후 대조, 배선 아님)
> **Task**: episode (검증) / selection(대상)
- 변경: `task_episode/verify_cutin_obj3d.py` — 선별 산출(event_select/winevent_select)의 VLM cut_in 후보를 `taxo_detect.detect_taxonomy`(obj3d corridor)로 사후 대조. **task_selection엔 미배선**(Stage1은 obj3d-free 유지 확정).
- 결과(동일 300):
  - **몽타주 VLM cut_in**: 전체 31개 중 obj3d confirm 2 → **corroboration 6%**, top50 13개 중 **0/13(0%)**. → 사용자 지적("cut_in 대부분 오류") 정량 확증.
  - **윈도우+video**: cut_in 주장 자체가 31→3(전체)·13→3(top50)로 급감, confirm 1/3. 오검 대폭 감소.
  - **obj3d 기저율**: 전체 300 중 cut_in계열 7개(cut_in 2+attempt 5)=~2.3% → cut_in은 실제로 희소(gold 0/50·corridor 26/1966과 정합). 몽타주 10% 주장은 과검.
- 결론: 윈도우+video가 과검을 대부분 제거. 잔여 미세 cut_in/attempt 정밀 확정은 **obj3d를 권위로**(Stage2/task_episode에서), Stage1은 recall triage로 유지.

## [2026-07-31] 폴더별(조건별) 반응성 선별 알고리즘 (folder_selection)
> **Task**: selection
- 변경: `task_selection/folder_selection.py` 신규 — 데이터가 정적환경 조건 폴더(주간/야간·도심/골목)로 분리 저장된 경우, **폴더 안에서 ego motion에 영향 준 event(반응성)** 가 있는 clip 우선 선별.
- 설계: 패러다임 (B) egomotion+VLM 융합 유지. ego 반응성 점수(`react_ego_score`) = 급제동 2.5·min(harsh,4) ≫ 감속반복·정지출발 > 정지 > 차선변경 > 회전(0.6). VLM(`vlm_reactive`)은 조건 고정이라 맥락 판정 대신 **외부 agent/hazard 반응 여부** 확인(신호대기 routine 정지 배제, event_type enum). `combined=max(ego_norm,vlm)+0.3·min`, **정규화·랭킹은 폴더 단위**.
- 이유: 사용자 지시 — 조건별 폴더 전제에서 "ego 전이/반응이 있는 critical clip" 우선. Stage1(전역 흥미도 triage)과 **별개**(조건 고정 → VLM 역할이 맥락→반응확인으로 이동).
- 입력: `groups={조건:[clip_id]}` 또는 `groups_from_root(루트/<조건>/<clip>)`. 실행 `./run.sh task_selection/folder_selection.py <groups.json> [top_k]`.
- 검증: 합성 2폴더×4클립 end-to-end OK — 폴더별 독립 랭킹, 반응성 이벤트 분해(harsh_brake/decel_repeat/stop_go), ego 강하나 VLM 미확인 clip은 결합점수 하향(융합 의도대로).

## [2026-07-31] 정적환경 VLM 판정 추가 (흡수 태그 검출 배선)
> **Task**: episode(Track2)
- 변경: `vlm_verify._schema`/`_prompt`에 정적환경 필드 추가 — lighting(day/twilight/night)·weather(clear/rain/snow/fog)·road_surface(dry/wet) 단일택 + glare/crosswalk_present/traffic_light_present/undivided_road bool. 공용 매핑 `env_cats(v)`→taxonomy 키. `verify_clip`·`candidates._vlm_present` 양쪽에서 호출. `CTX`에 정적환경 키 추가(fuse에서 VLM 권위·GT 없이 유지).
- 이유: STATUS=vlm_only 정적환경 13태그(조명/기상/노면/glare/crosswalk/신호등/비분리)에 실제 판정 경로 부여. GT 부재 맥락이라 VLM이 유일 생성원(∩ 불필요, 다수결/단독).
- 검증: 스키마 유효·env_cats 매핑 OK, VLM 서버 1클립 end-to-end에서 twilight/clear_weather/dry_road/crosswalk_present/traffic_light_present 판정·매핑 확인.
- 미포함(후속): obj3d 기반 신규 태그는 obj3d GT 업데이트 확인 후. road_worker/vulnerable_pedestrian(이벤트, vlm)은 present enum 확장 별건.

## [2026-07-31] new_tag.json(v0.4 폐쇄어휘) 정적환경+long-tail 흡수
> **Task**: episode(Track2) + common
- 배경: 정적환경 태깅을 성급히 제거했다가(같은 날) 되돌림 — 최종 목적이 **search 기반 학습데이터 큐레이션**이라 정적환경 태그가 필요. `legacy/new_tag.json`(condition 25 + event 25, GT rule·cell_role·3값판정·backoff 갖춘 성숙 어휘)을 확인.
- 결정(옵션2): **new_tag 정본 채택 대신 taxonomy.py 구조 유지하며 흡수**. 검출기 배선 보존, GT rule 세부 미채택.
- 변경(`common/taxonomy.py`): 새 축 **정적환경**(조명 night/twilight/day·기상 rain/snow/fog/clear·노면 wet/dry·glare·crosswalk_present·traffic_light_present·undivided_road·crowd) + long-tail 이벤트(vehicle_cross_path·wrong_way·stationary_vehicle·large_vehicle·emergency_vehicle·animal·road_obstacle·jaywalking·road_worker·vulnerable_ped). 조명/기상/노면 9 승격. **KEYS 41→60**.
- **egomotion 기반 제외(사용자 결정)**: hard_brake/hard_steer/overtake/sharp_curve/congestion/free_flow 는 별도 태그로 추가 안 함 — egomotion primitives/기존 tag로 병합(congestion→`creep`, 급제동/급조향→events decelerate/turn·harsh_decel, overtake→lane_change). 추가 어휘로 미사용.
- **obj3d 기반 신규(사용자 결정)**: 어휘만 유지, 검출기는 obj3d 결과 확인 후 도입(deferred).
- `STATUS` dict: 흡수 태그별 visionary 실행상태(runnable/vlm_only/sparse/gold). ODD(조명/기상/노면 dim)는 검색 정본을 정적환경 축에 넘기고 per-clip 단일값 표현으로만 잔존.
- 영향: gold 도구 정적환경 축 노출(chips↑), 활성 import 9/9 OK, KEY 유일성 OK. 미구현 후속 = obj3d 신규 태그 검출기 + 정적환경 VLM 판정 + new_tag 3값판정/FDR 프레임 도입 여부.

## [2026-07-31] 드리프트 4건 코드 정합화 (설계 불변식 반영)
> **Task**: multi (Track1+Track2)
- 변경:
  1. `tag_v08.V08_SCHEMA` 순방향 재정렬 — required=`[scene_description, critical_components, chain_of_causation, cause, ego_action]`(MA-last). guided decoding이 관찰 선행 후 MA를 마지막 커밋. rec 출력도 동일 순서. ego_action은 `_ground_ego_action`(rule/arc 앵커) 유지.
  2. `taxonomy.AUTO_GT` = `{stop}`(순수 종방향 kinematic만). turn/u_turn류는 `HUMAN_KEYS`(맥락해소·평가대상)로 이동. `auto_tags_from_arc`는 Phase A recall 후보로 turn 계속 방출(주석 명시).
  3. `retrieve.MERGE`에서 `intersection_signalized/unsignalized` 제거 — sig/unsig 별도 유지. 도로유형(`road_urban_arterial/backstreet→road_surface`) 병합만 존치.
  4. Track2 인덱스에 **cause 축** 추가 — `candidates._cause_candidates`(카테고리→cause 사상, channels·vote_fraction 승계, 증거없음→other) → `retrieve._cause_axis`가 `index_clip` 에피소드에 `cause:[{cause, confidence, channels, from}]` 노출.
- 이유: CLAUDE.md §2 확정 불변식(순방향 SD→SA→MA / 두 층위 ego_action / cause=1차 query 키 / sig·unsig=cause 결부)과 코드 정합. [[ego_action 두 층위]]·[[cause 축]]·[[Track2 검색 입도]] 반영.
- 영향: `tag_v08.py`·`taxonomy.py`·`retrieve.py`·`candidates.py`. 스모크 테스트 4/4 통과, import ACTIVE 16/16. cause 단일 확정·상호작용 gt∩vlm은 Phase C 미구현(후속).

## [2026-07-31] 저장소 정리: legacy/docs 분리, 활성/legacy 판별 가능화
> **Task**: multi
- 변경: 활성이 import 안 하는 구세대(`tagger`·`vocab`·`prompts`·`window/event_tagger`·`test/batch_readout`·`norm_embed`)를 `legacy/`로, 설계문서를 `docs/`로 분리. `build_client_pool`을 `common/client.py`로 추출. `legacy/docs/decisions`는 공통 폴더 + 파일별 Task 라벨.
- 이유: 데모용으로 관리 없이 작성돼 파일 트리로 용도 판별 불가했음. 타입별 공통 폴더 + Task 라벨로 중복 없이 정리.
- 영향: `.pth`/`run.sh` PYTHONPATH에 `legacy/` 추가(import 21+8 OK). 각 폴더 README.

## [2026-07-31] 저장소 구조: common / task_selection / task_episode 물리 분할
> **Task**: multi
- 변경: flat 모듈을 2단계 task 기준 폴더로 이관. Task1 선별을 `selection.py`로 모듈화.
- 이유: labeling이 ① clip 선별 → ② episode 추출 2단계. 공유 기반과 task별 알고리즘 분리.
- 영향: import는 PYTHONPATH+`.pth`로 top-level 유지. 스키마 json은 vocab 모듈과 동거.

## [2026-07-31] VLM self-consistency 투표 — 힌트 유지(B), 상호작용 합의는 불신
> **Task**: episode(Track2)
- 변경: Phase A VLM 5-vote는 GT 힌트 유지하되 상호작용 "합의"를 독립확인으로 신뢰 안 함.
- 이유: 힌트가 상호작용만 나열 → 맥락 투표는 이미 독립. 상호작용은 Phase C에서 `gt∩vlm`, 맥락은 다수결로 분리 처리.
- 영향: `candidates.py`, Phase C 설계.

## [2026-07-31] Track2 검색 입도 — 교차로 sig/unsig 유지, 도로유형만 병합
> **Task**: episode(Track2)
- 변경: `retrieve.MERGE`의 sig+unsig→intersection 병합을 정정 대상으로. 도로유형만 병합.
- 이유: 신호/비신호는 cause와 결부(비신호=양보 정차 vs 신호=신호 정지) → 병합 금지. 병합 규칙=(a)애매 ∧ (b)query/cause 무가치.
- 영향: `retrieve.py`, `docs/taxonomy_merge_report.md`.

## [2026-07-31] cause 축 — 전이의 "왜", GT+맥락, Track2 1차 query 키
> **Task**: multi (Track1+Track2)
- 변경: cause=전이의 "왜", 모든 값 GT+맥락 해소. **Track2 인덱스에 cause 축 추가 필요**(현재 갭).
- 이유: agent조차 tracking만으론 인과 판정 불가. 큐레이션은 주로 cause로 검색.
- 영향: `candidates.py`/`retrieve.py`, `taxonomy.py`.

## [2026-07-31] ego_action — 두 층위 + 앵커 + 순방향 생성 (invariant 정정)
> **Task**: multi (Track1+Track2)
- 변경: 종방향(accel/decel/stop/creep)=egomotion rule 확정 / 경로(turn·lane_change·u_turn)=CAN 전이 트리거 + 라벨은 맥락 해소(map∪VLM). 앵커=전이 감지(→key_frame·세그먼트). 생성은 순방향 SD→SA→MA, MA는 마지막 해소.
- 이유: CAN yaw만으론 교차로 turn/커브/분기/u-turn 구분 불가(under-determined).
- 영향(드리프트 수정 대상): `tag_v08.V08_SCHEMA` MA-first→forward + ego_action 앵커화; `taxonomy.AUTO_GT`에서 turn류를 맥락해소 계층으로.

## [2026-07-31] 세그먼테이션 = ego 전이 = critical scenario
> **Task**: multi
- 변경: 세그먼트를 ego_action 전이로만 정의. non-reactive는 critical 아님. 양 track 동일 백본.
- 이유: critical=ego 반응으로 확정(추후 revisit). `key_frame_t`=rule.
- 영향: `classify073.consolidate_episodes`.

## [2026-07-31] 산출물 두 제품(Track1/Track2) 병존
> **Task**: multi
- 변경: Track1=v08 SD/SA/MA 메타데이터(라벨링) + Track2=v3 taxonomy 추출(큐레이션). 공용 기반 공유.
- 이유: 라벨링과 큐레이션은 다른 산출물. v3가 드롭했던 SD/SA/MA(Track1)를 명시 유지.
- 영향: `tag_v08` vs `candidates/retrieve`.

## [2026-07-31] v3 관점 전환 — 태거 = retriever (recall 우선)
> **Task**: episode(Track2)
- 변경: 태거=관대한 후보 생성기. Phase A(OR 앙상블·∩ 금지)→B(정규화·병합·랭킹)→C(precision 하류)→D(완전 gold).
- 이유: 큐레이션은 recall이 자산, recall miss>FP. 실측 OR합집합 0.81(병합후 0.90)>VLM단독 0.67.
- 영향: `candidates.py`·`retrieve.py`. v2(∩ precision-우선) 폐기.

## [이전] 2단계 퍼널 · map 재해석 · cut_in 희소 등
> **Task**: multi
- Stage1(CAN+VLM 선별)/Stage2(3DOD+map). map centerlines=경계선(재해석·유효율 35%·is_intersection 죽음). cut_in 극희귀(0/50, ~26/1966). obj3d vx/vy·occlusion 불신. 상세 `docs/PROJECT_DESIGN.md`·`task_selection/SELECTION_STAGE1.md`.
