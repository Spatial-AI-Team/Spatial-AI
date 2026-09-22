# CLAUDE.md
## Language Policy
- 모든 응답은 한국어 또는 영어로만 작성한다.
- 한자(漢字)나 일본어 문자(히라가나/가타카나) 혼용을 절대 금지한다.
- 상태 메시지(대기, 완료, 계획 등 진행 보고 문구)도 예외 없이 순수 한국어만 사용한다.
This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

자율주행 클립에서 **자차 거동 사건(에피소드) 메타데이터를 태깅**하고 long-tail 상황을 **추출**하는 파이프라인(KATECH VLA). VLM(NVIDIA Cosmos-Reason)은 **frozen** — 재학습하지 않고 GT(egomotion/obj3d/map)+규칙+VLM 조합 로직을 개선한다.

**문서는 3층이다. 혼동하지 말 것.**

| 층 | 내용 | 상태 |
|---|---|---|
| §1 | 현재 코드 실태 | **실제로 존재하는 것** |
| §2 | 목표 설계 (지침서 v0.4) | **아직 구현되지 않음** — 신규 작업의 방향 |
| §3 | 불변 규칙 | 두 층 모두에 적용 |

§2를 근거로 기존 코드가 그렇게 동작한다고 가정하지 말 것. 신규 설계·리팩터링은 §2를 따르고, 기존 코드 수정은 §1을 확인한 뒤 §4의 전환 규칙을 따른다.

---

# 1. 현재 코드 실태

## 2단계 퍼널
- **Stage 1 선별**: 전체 로그(**CAN+video만**, obj3d/map 없음) → ego arc + VLM 흥미도(몽타주) 결합 랭킹 → 상위 K. recall 위주 triage
- **Stage 2 에피소드**: 선별 clip(**3DOD + map** 사용) → egomotion 전이로 분할 → 에피소드별 메타데이터
- 근거: 싸게 넓게 선별하고, 비싼 상세 태깅은 선별분에만

## 폴더 구조
```
common/          공유 인프라: config·paths·dataset·events·taxonomy·vocab073·client·overlay + schema
task_selection/  Stage1: selection.py + SELECTION_STAGE1.md → gold_label/select/
task_episode/    Stage2: base   classify073(consolidate)·taxo_detect·map_lane·vlm_verify
                        Track1 tag_v08          Track2 candidates·retrieve
deploy/          Stage1 배포 패키징 + schema/(INTERFACE_SPEC·JSON Schema·SHARE.md)
legacy/          활성이 import 안 하는 구세대(v0.7.1 tagger·vocab·prompts·window/event_tagger 등)
docs/            PROJECT_DESIGN(정본)·TEAM_REPORT·taxonomy_merge_report·STRUCTURE
decisions/       DESIGN_LOG
```
- import는 `run.sh` PYTHONPATH + venv `.pth`가 `common/task_selection/task_episode/legacy`를 top-level로 올려 유지
- Task 라벨 컨벤션: `legacy/docs/decisions` 각 파일 최상단에 `> **Task**: common|selection|episode(Track1|Track2)|multi`

## 명령어
정식 test/lint 없음. `test_readout.py`는 단위테스트가 아니라 단일클립 smoke run.
```bash
./run.sh <script.py> [args]        # venv 실행 + logs/latest.log
tail -f logs/latest.log
.venv/bin/python <script.py>
./run.sh batch_readout.py --limit 5 [--index i] [--overwrite] [--no-overlay]   # v0.7.1 경로 → outputs/tags/
.venv/bin/python -m pip install <pkg>
for p in 8001 8002 8003 8004; do curl -s -o /dev/null -w "$p %{http_code}\n" localhost:$p/v1/models; done
python3 -m http.server 8080 --bind 127.0.0.1 --directory gold_label
docker container logs <container>  # 전체형
```
임시 산출물은 `$CLAUDE_JOB_DIR/tmp` (공용 `/tmp` 금지 — 병렬 job 충돌).

## 파이프라인 4세대 공존 (혼동 주의)
- **v0.7.1** `tagger.py`+`prompts.py`+`vocab.py`+`window_tagger.py`+`event_tagger.py` — **유일한 runnable 엔트리**(`test_readout.py`, `batch_readout.py`)가 사용
- **v0.7.3** `classify073.py`+`vocab073.py` — 2-pass 분류기. `consolidate_episodes()`는 상위 공용
- **v0.8** `tag_v08.py` — SD + critical_components. `outputs_v08/tags/`
- **v3** `candidates.py`→`retrieve.py`(+`taxo_detect`/`vlm_verify`/`map_lane`/`taxonomy`) — recall-우선 retriever. ad-hoc 실행

## 두 제품
- **Track1** v08형 세그먼트 메타데이터(SD/SA/MA + cause + key_frame_t) — 라벨링. 계보 `tag_v08.py`
- **Track2** v3 추출 retriever(taxonomy 후보 검색) — 큐레이션. `candidates.py`→`retrieve.py`
- 공용 기반: `events.py`·`taxo_detect.py`·`map_lane.py`·`vlm_verify.py`·`classify073.consolidate_episodes`

## v3 검색 파이프라인 (Track2)
- **Phase A** `candidates.py` — 3채널 OR 합집합(ego-arc / obj3d-GT / VLM 5-vote temp>0), provenance·vote_fraction·multi 부착
- **Phase B** `retrieve.py` — 정규화 + 병합 + confidence 랭킹. 병합 원본은 `sub` 보존
- **Phase C/D 미구현** — precision 게이트 + 완전라벨 gold
- 실측 recall(gold 50): OR합집합 0.81(병합후 0.90), VLM단독 0.67, GT단독 0.17

## 신호 채널
- `events.py` — egomotion 거동. **turn은 yaw 크기가 아니라 net-heading 변화량**(u_turn 미검출, 방향 L/R 통합). egomotion 로그가 비디오보다 길어 카메라 timestamp 범위로 클립
- `taxo_detect.py` — obj3d 상호작용. `detect_taxonomy()`
- `map_lane.py` — Branch A(lane-relative)/B(corridor fallback), `map_valid()` 게이트(유효율 ~35%)
- `vlm_verify.py` — `_prompt`/`_schema`(guided_json), `fuse()`(∩), `ground_signals()`
- `taxonomy.py` — `AUTO_GT`/`HUMAN_KEYS`/`auto_tags_from_arc`

## 데이터 규약 & 함정
- **두 데이터셋**: `config.DATASET_ROOT`=구 `PhysicalAI-AV-curated`. **활성은 `paths.py`의 `visionary-nvidia`**(obj3d+map+egomotion). 신규 코드는 `paths.py`
- **obj3d**: lidar frame `x=전방, y=좌우`, `boxes_3d`=11-DOF. **vx/vy 16%만 유효**(위치미분 사용), occlusion NaN, 일부 clip `track_id` 컬럼 누락
- **map `centerlines`=실제 차선 경계선**(명칭 오류). ego차로=y=0 사이 인접 경계쌍. **`is_intersection` 전부 0**, `timestamp_us`=0→frame_idx 선형매핑
- **VLM 호출**: `guided_json` enum 강제. `TEMPERATURE=0`; n-vote는 temp>0 명시. `video_url`(subclip data URI, `SEND_FPS=10`이나 서버 ~12프레임 캡) 또는 `image_url`(몽타주)

---

# 2. 목표 설계 (지침서 v0.4) — 미구현

정본: `docs/design/pipeline_design_guide_v0.4.md` > `common/schema/tag_vocab_v0.4.json` > `실험계획_추론경로_260824.md`(2026-09-14 확정: main `docs/experiments/experiment_design_v0.1.md`가 정본. worktree에 2026-08-24 초안이 별도로 존재했으나 사용자가 main 최종 저장본을 정본으로 확정. 경위는 `decisions/DESIGN_LOG.md`의 `[2026-09-11] 실험계획_추론경로_260824.md 실체 확인`·`[2026-09-14] experiment_design_v0.1.md 정본 확정` 항목 참고).
충돌 시 위 순서를 따른다. 어휘 JSON을 코드에 복제하지 말고 단일 원천에서 로드한다.

## 목적 우선순위
- **라벨 품질이 측정 편의에 우선.** 태깅 시점 확보 정보는 제한 없이 사용한다
- 측정 목적 제약(독립성·전파율 분리)이 라벨 품질을 낮추면 측정 쪽을 조정한다 — 표본 한정 이중 실행, 조건 분리 보고
- 전역 금지로 둘 수 있는 것은 순환성·재현성처럼 어기면 결과가 무효가 되는 항목뿐

## 산출 단위
- 1 에피소드 = **1 자차 거동 사건** = (자차 거동, 원인 집합) 쌍. 원인 복수 허용, 순위 없음
- 계층: clip(20~30초 저장 단위) ⊃ episode(거동 사건 구간, clip당 0~수 개) ⊃ subject(원인 후보 객체)
- 저장 2블록: 에피소드 공통(시각·환경·자차 거동·인과 링크) + `subjects[]`(객체별)
- 시간창은 **거동 완료·안정화 시점까지** 포함 (거동 개시에서 끊지 않음)

## 4단계
```
S0 앵커 검출   CAN 규칙 → key_frame_t, 자차 거동 전이 (전이 검출 4중 필터)
S1 물리 사실   3DOD + 지도 + CAN → 위치·거동·계측값·원인 후보 집합
S2 시각 의미   VLM → 등화·신호·표지·노면·가시성·의도 단서
S3 인과 귀속   VLM → 원인 집합 확정, 신뢰도
```
- **S1·S2는 병렬, S3에서 합류.** 순차 체인 아님 → 오류 전파율은 S1→S3, S2→S3 **두 경로** 분리 산출

## 전이 검출 4중 필터 (S0)
사건이 아닌 변화(도로 곡률 추종·차로 내 흔들림·노면 요철·가다서다)를 거른다. 임계값은 외부화하되 **구조는 고정**.

1. **크기** — 종방향은 속도 변화량·감속도, 횡방향은 **누적 방위 변화량**(순간 요레이트 아님)
2. **지속** — 최소 지속시간 미만의 스파이크 제외
3. **곡률 보정** — 자차 방위 변화에서 도로 기하가 설명하는 몫을 뺀 **잔차**로 판정. 보정 출처(`map`/`estimated`/`none`) 기록
4. **병합** — 같은 방향 전이의 짧은 연속은 1건으로. 반대 방향은 병합 안 함. 앞 필터 통과분끼리만

기록: `transition_filters_passed`·`curvature_correction_source`·`merged_from`
한계: 필터 미통과 변화는 에피소드 미생성 → **경계 탈락 사례를 별도 로그로 축적**(임계 조정 근거)

## 규칙 불확실성 신호 (S1)
규칙 판정이 흔들리는 지점을 VLM 호출 전에 결정론적으로 판별한다.

| 신호 | 계산 |
|---|---|
| 지도 유효성 | 차로 경계쌍 폭이 정상 범위 + 창 내 일정 비율 유지 |
| 기하 여유 | 판정 경계까지 거리 ÷ 차로 폭 (경계 근접 시 차로 귀속 불안정) |
| 검출 품질 | 관측 프레임 비율 · 박스 변동폭 · 겹침 |
| 소스 상충 | 코리도어 근사 ↔ 지도 판정, 횡변위 ↔ 차로 인덱스 불일치 수 |
| 시계열 흔들림 | 창 내 판정 라벨 변경 횟수 |

용도: ① gt_before 주입 제어 ② VLM 라우팅(전 신호 잠잠 → 질의 생략) ③ 신뢰 점수 원료
**v1은 지도 유효성 + 기하 여유 2개만 가동**, 나머지는 값만 기록 (동시 튜닝 시 근거 없는 상수 증가)

## S1 물리 정보의 S2 주입
| 클래스 | 필드 |
|---|---|
| `gt_only` (호출 없음) | 자차 속도·가속도·요레이트, 거리·방위, 차로 ID, 지도 기하, 계측값 전량 |
| `gt_before` (영상 앞) | 객체 목록(ID·종류·좌표·속도), 자차 기준 위치, 객체 거동, 자차 전이 |
| `gt_free` (미주입) | 등화류·표식·노면·가려짐·의도 단서·보행자 자세/시선, 날씨·조명·도로 유형 |

- **gt_before는 관측 사실만** — "감속 중"은 관측, "위험"·"끼어드는 중"은 판정이므로 제외
- 영상 뒤 주입은 기본값 미채택(금지 아님). 실험 C에서 이득 확인 시 채택

## 물리 게이트 6검사 (B)
산출은 **오답 확정 / 미판정** 두 값뿐. **정답 판정 없음, 통과율을 정확도로 보고 금지.**

| 검사 | 로직 |
|---|---|
| 시간 순서 | 원인 상태변화 시각 < `key_frame_t` |
| 반응 지연 | `key_frame_t − t_c`가 원인 유형별 [하한, 상한] 내 (**단일 상수 금지** — 신호 대기와 끼어들기 대응의 지연 분포가 다름) |
| 기하 정합 | 원인이 자차 경로·관련 인접 영역 내 |
| 근접성 | 간격·TTC·측방 여유가 후보 범위 내 |
| 가시성 | 어느 센서로도 미관측이면 원인 불가. 카메라 비가시·LiDAR 단독은 통과 + 플래그 유지 |
| 유형-방향 정합 | 원인 유형과 자차 거동 방향의 정합 |

위반 시 **라벨 삭제 금지** — 위반 항목을 기록해 오류 분류·재라벨 우선순위에 사용

## 입력 조립
```
[1] system 지시      ─┐
[2] 태그 어휘         ├ 전역 정적 블록 (전 클립 동일, 결정적 직렬화)
[3] few-shot          ─┘
[4] GT-before         ─┐ 클립 프리픽스 (관측 사실만 — 판정값 금지)
[5] 영상 토큰         ─┘
[6] 필드 지시 + 후보     필드별 꼬리
```
- [1]~[3]에 가변 요소(클립 ID·타임스탬프·검색형 few-shot·무작위 정렬) 혼입 금지
- 어휘 변경은 전역 블록을 무효화 → 릴리스 단위로 묶고 변경 시점을 배치 경계로 기록

## 점수화 실행 (4단)
| 단 | 대상 | 조건화 |
|---|---|---|
| T1 | 객체 종류·위치·환경·통제 요소·시각 속성 | 클립 프리픽스만 (전량 병렬) |
| T2 | 결과 상태·객체 거동·기하/노면 유형·관련성 | T1 결과 포함 |
| T3 | 원인 유형·인과 링크 | T1+T2 결과 포함 |
| T4 | 자유 서술 | T1~T3 확정값 포함, decode 1회 |

- 다중값 필드는 조합 나열 금지 → **후보별 이진 채점**, 순서는 계측 시각으로 결정
- 후보 길이 차 보정을 위해 **길이 정규화** 후 비교. 후보는 프롬프트 **말미에만** 배치(캐시 경계)
- 기본 점수화 방식 = **시퀀스**. first-token은 충돌 없음이 검증된 필드만

## 설정으로 열어둘 것 (하드코딩 금지)
미결 항목이 코드에서 조용히 확정되는 것을 막는다. 기본값을 지정하되 미결임을 주석에 남긴다.

| 스위치 | 값 | 기본값 |
|---|---|---|
| 물리 정보 가공 수준 | 원측정값 / 파생 판단 | 미정 |
| 시간창·프레임률 | 고정 / 거동 적응 | 고정 |
| 뷰 선택 | 고정 뷰셋 / GT 기반 적응 | 고정 |
| 중간 산출물 외부화 | 2-pass / 단일 pass | 2-pass |
| 필드별 추론 경로 | gt_only / scored / generated | 어휘에서 로드 |
| 점수화 방식 | 시퀀스 / first-token | 시퀀스 |
| 조건화 강도 | strong / weak | strong |
| GT 주입 배치 | gt_only / gt_before / gt_free | 어휘에서 로드 |
| 임계값 전체 | — | `threshold_set_id`로 외부화 |

## 산출물에 반드시 남길 것
- **후보 집합 전체**(선택되지 않은 것 포함) — 누락 오류와 선택 오류의 구분 근거
- **중간 산출물**(S2 출력) 영속화 — 오류 귀속 분리의 전제
- 플래그: `map_valid`·`fallback_path`·`camera_visible`·`window_truncated`·`boundary_reason`·`views_used`·`budget_profile`
- 재현성 매니페스트 5군: 모델(체크포인트 해시·토크나이저·dtype·병렬 수) / 서빙(배치 상한·프리픽스 캐시·CUDA graph·프레임워크 버전) / 디코딩(seed·temperature·top_p·최대 토큰·추론 경로) / 입력(정적 블록 해시·few-shot 버킷·프레임 수·프레임률·해상도·뷰) / 데이터(GT 버전·어휘 버전·`threshold_set_id`)
- **seed 고정만으로는 재현되지 않는다** — 배치 크기·캐시 히트에 따라 부동소수점 합산 순서가 달라짐

---

# 3. 불변 규칙 (DO NOT VIOLATE)

## Frozen tagger / anti-circularity
- 태거 **fine-tuning 금지**(순환). **cosmos2(port 8000) 무접촉**(타 그룹). 복제본 8001–8004 라운드로빈, **8001은 평가-frozen** — 8002–8004의 동일 가중치 여부 유지 확인
- NIM 컨테이너 파일 수정 시 blake3 체크섬으로 컨테이너 사망 → 재기동 승인 필요
- 태거 출력이 태거 학습에 되먹임 금지. gold는 사람 라벨(태거 파생 아님), `AUTO_GT`는 평가 제외
- KV-cache·TTA·합성데이터·다운스트림 VLA 학습은 범위 밖

## 설계 원칙 5
1. **앵커 비오염** — `key_frame_t`와 자차 거동 **전이 검출**은 CAN 규칙 산출. 모델 출력이 중간 경유로도 진입 금지
2. **판정 근거 시점 표기** — 판정에는 창 전 구간을 사용한다(사후 정보 제한 없음 — 목적은 최선의 라벨). 산출물에 사후 정보 사용 사실을 표기하여 예측 모델 학습 시 소비 측이 인지하도록 한다
3. **순환 배제** — 기본 프레임 샘플링과 뷰 축소를 원인 후보에 의존시키지 않는다. 후보 기반 **추가** 프레임·뷰 확대는 플래그 기록 후 허용
4. **무기록 fallback 금지** — 대체 경로(지도 실패 시 근사 등) 사용은 반드시 플래그로 기록
5. **필요조건과 정답의 구분** — 물리 게이트 통과를 정확도로 보고 금지. 게이트 산출은 `오답 확정`/`미판정` 두 가지뿐

## 세그먼테이션
- 세그먼트는 **ego 전이로만 정의.** ego에 영향 없는 상황은 세그먼트 미생성 (한계로 문서화, 추후 revisit 가능)
- `key_frame_t`는 rule 기반(egomotion 전이 onset). 모델 생성 금지
- `1 record = 1 transition = 1 behavior event`

## 자차 거동 — 검출과 라벨 해소의 분리
- **전이 검출 = CAN 규칙 확정.** 종방향(accel/decel/stop/creep)도 CAN 확정
- **경로 라벨(turn_L/R·lane_change_L/R·u_turn)은 CAN만으로 under-determined** — 교차로 회전 / 커브 / 분기 / U턴 구분 불가. 라벨은 **map ∪ VLM으로 해소**
- 즉 "자차 거동은 rule anchored"의 정확한 의미는 **전이 시점과 종방향은 규칙 확정, 경로 유형은 맥락 해소**다. 지침서 v0.4의 "전량 CAN anchored" 서술은 이 구분으로 정정한다
- 생성은 순방향 — 관찰 선행(scene → components → …), MA는 앵커+맥락으로 마지막 해소. behavior-first 금지. `tag_v08.V08_SCHEMA` MA-last 정렬 완료(2026-07-31)

## VLM 프롬프트의 GT 힌트
- 현재: 상호작용 힌트 유지(상호작용 확인용). **상호작용의 "multi 합의"는 독립 확인으로 신뢰하지 않음** — Phase C에서 상호작용은 `gt∩vlm`, 맥락은 VLM 다수결로 분리 처리
- 목표 설계(§2)는 GT-before에 **관측 사실만** 넣고 판정값 주입을 배제한다. 두 방침이 다르므로, 신규 구현 시 §2를 따르고 기존 경로 변경 시에는 실험(전후 비교) 없이 바꾸지 않는다

---

# 4. 전환 규칙 — 현재 코드와 목표 설계의 차이

신규 코드는 §2를 따른다. 기존 코드를 §2로 옮길 때는 아래 차이를 먼저 확인한다.

| 항목 | 현재 | 목표 | 처리 |
|---|---|---|---|
| cause 값 집합 | 4값(agent/signal/road_geometry/other) | 6값(+static_object, road_condition, ego_intent, signal→traffic_control) | 매핑표 작성 후 일괄 전환. 기존 산출물은 `schema_version`으로 구분 |
| 제품 구조 | Track1(라벨링) / Track2(검색) 2제품 | 단일 에피소드 산출물 + 파생 뷰(검색 별칭) | Track2의 검색 카테고리를 파생 뷰 정의로 이관 가능한지 검토 후 결정 |
| VLM 출력 방식 | guided_json 생성 | 닫힌 집합은 점수화, 자유 서술만 decode | 실험 A 결과 확인 후 전환. 그 전에는 병행 |
| 시간창 | 서버 ~12프레임 캡, subclip | 거동 완료까지 + 프레임률 변수 | 캡이 상한이므로 창 길이·프레임률 교환 설계 필요 |
| GT 힌트 | 상호작용 힌트 주입 | 관측 사실만 | 실험 C 결과 확인 후 전환 |
| 어휘 | vocab073 / taxonomy | `tag_vocab_v0.4.json` | 병행 기간 동안 `schema_version` 필수 |

**전환 원칙**: 한 번에 하나씩. 1 브랜치 = 1 변경 축. 정본 병합 요건은 변경 내용·가설 / 대조 조건 / 동일 평가셋 결과 / 회귀 확인.

---

# 5. 운영

- `docker container logs`(전체형), `python3`, venv(via `uv`) — 터미널 세션마다 재활성화
- 신규 데이터 접근은 `paths.py`(visionary)
- 임시 산출물은 `$CLAUDE_JOB_DIR/tmp`
- 막히면 진행을 멈추고 블로커 리포트를 남긴다
- 설계 변경은 `decisions/DESIGN_LOG`에 기록

## 에이전트 위임 (`docs/design/AGENT_DESIGN.md` 정본, 2026-09-09 반영)

메인 세션은 오케스트레이터다. 아래에 해당하는 요청은 직접 구현하지 않고 위임한다.

```
어휘·스키마·필드·값 집합·버전·린트                    → schema-keeper
전이·앵커·에피소드 분할·3DOD·지도·위치·계측·후보        → rule-engineer
프롬프트·VLM·점수화·생성·서빙·매니페스트                → vlm-engineer
러너·인수인계 경로·Stage1↔Stage2 배선·산출물 영속화     → pipeline-integrator
게이트·술어·역검증·채점·지표·전파율·calibration·리포트  → verifier
스모크셋·gold·라벨링 도구·IAA·변환                     → dataset-curator
실험·arm·브랜치·재현                                  → experiment-runner
결정·기록·번복·외부 문서 반영                          → design-scribe
다음에 무엇을 바꿀지·성능 정체·오류 분포 해석            → design-optimizer
진행 불가·오류 원인 불분명·반복 실패                    → blocker-triage
```

한 요청이 생산+검증 두 축에 걸치면 분리해서 순서대로 위임한다. 위임하지 않고 메인이 직접 하는 것: 우선순위 판단, 에이전트 간 충돌 중재, 사람에게 결정 요청.

**자동 사이클, 사람 승인은 끝 1곳**: task 정의 → 카드 자동등록(experiment-runner, `experiments/cards/`) → 실행·계약 경로 영속화(pipeline-integrator, `outputs/`) → 게이트+gold 채점+회귀(verifier, `experiments/results/`+`reports/`) → 변경 제안서(design-optimizer, `experiments/proposals/`) → **사람이 리포트+제안서 리뷰(유일한 결정 통로)** → 결정 기록(design-scribe, `decisions/DESIGN_LOG.md`) → 다음 사이클. 상세는 `AGENT_DESIGN.md` §6.
