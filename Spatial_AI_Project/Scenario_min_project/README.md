# KATECH VLA — 자율주행 에피소드 태깅 & Long-tail 추출

자율주행 클립에서 **자차 거동 사건(에피소드) 메타데이터를 태깅**하고 long-tail 상황을 **추출**하는 파이프라인.
VLM(NVIDIA Cosmos-Reason)은 **frozen** — 재학습하지 않고 GT(egomotion/obj3d/map)+규칙+VLM 조합 로직을 개선한다.

## Architecture — 2단계 퍼널

- **Stage 1 선별** (`task_selection/`): 전체 로그(CAN+video만, obj3d/map 없음) → ego arc + VLM 흥미도(몽타주) 결합 랭킹 → 상위 K. recall 위주 triage
- **Stage 2 에피소드** (`task_episode/`): 선별 clip(3DOD + map 사용) → egomotion 전이로 분할(S0) → 물리 사실(S1)·시각 의미(S2)·인과 귀속(S3)

싸게 넓게 선별하고, 비싼 상세 태깅은 선별분에만 적용하는 구조다. S0(CAN 규칙 앵커 검출)는 Stage1·Stage2·Track1·Track2가 전부 `common/events.py`를 공유한다 — map 유무(`curvature_fn` 인자)만 다르다.

## 두 제품 — 현재 동작 상태

| 제품 | 내용 | 계보 | 상태 |
|---|---|---|---|
| **Track1** | 세그먼트 메타데이터(scene description/critical components + cause + ego_action) — 라벨링 | `task_episode/tag_v08.py` | **동작함**(vis100 100클립 검증, 98/100) |
| **Track2** | recall-우선 retriever(3채널 OR 합집합: ego-arc/obj3d-GT/VLM n-vote) — 큐레이션 검색 | `task_episode/candidates.py` → `retrieve.py` | **mp4 전용 경로 미해결 — vis100에서 0/100 실패**(프레임 갤러리 미대응) |

실측 recall(gold50, 2026-07 기준): OR 합집합 0.81(병합 후 0.90), VLM 단독 0.67, GT 단독 0.17. (gold50 원본 데이터는 이후 데이터셋 재구성으로 현재 디스크에 없음 — 아래 "실행 가능한 clip 세트" 참조)

## Stage2 단계별 코드 대응

| 단계 | 역할 | 파일 |
|---|---|---|
| S0 | 앵커 검출(CAN 규칙, egomotion) | `common/events.py` (`detect_events`) |
| S0 | 에피소드 분할·통합 | `task_episode/classify073.py` (`consolidate_episodes`) |
| S0 | 곡률보정·차선 유효성(map) | `task_episode/map_lane.py` |
| 러너 | Stage1↔Stage2 실행·산출물 영속화 | `task_episode/run_pipeline.py` (`CLIP_SETS={gold50,selected50,vis100}`) |
| Track1 (S1+S2+S3) | 융합 실행(생성 경로, guided_json) | `task_episode/tag_v08.py` |
| Track1 (S1, 실험) | 시퀀스 점수화 경로(A1) — 독립 함수, `run_pipeline.py` 미배선 | `common/scoring.py`, 검증 스크립트 `task_episode/exp_a_score_probe.py` |
| Track2 (S1+S2) | taxonomy 상호작용 검출 | `task_episode/taxo_detect.py` |
| Track2 (S2, 검증) | VLM 교차검증 | `task_episode/vlm_verify.py` |

## 데이터 계층 (`common/`)

| 역할 | 파일 |
|---|---|
| 데이터 경로 상수 | `paths.py` (`ROOT=/katech/datasets/visionary`) |
| 프레임 샘플링(mp4·프레임갤러리 자동판별) | `dataset.py` (`is_frame_gallery`·`sample_frame_sequence`·`clip_duration`) |
| VLM 호출 클라이언트 | `client.py` (`visual_content()` — video_url/image 시퀀스 분기) |
| 설정값 / 임계값 단일 원천 | `config.py` / `thresholds.py` |
| 어휘 | `taxonomy.py`·`vocab073.py`(활성) · `schema/tag_vocab_v0.4.json`(목표, 미전환) |
| GT 배치 3-way | `gt_placement.py` — **정의만 있고 어디서도 호출되지 않는 사장 모듈** |
| 재현성 매니페스트 | `manifest.py` |
| 오버레이 렌더(사후 리뷰용, VLM 입력 아님) | `overlay.py` |

## 검증·실험 인프라

```
verification/    물리 게이트(gate.py, 5술어 중 2개만 실효)·gold 채점(score_gold.py)·리포트(report.py)
experiments/      cards(사전등록)·results(exp별 산출)·proposals(변경 제안)
hooks/            pre-commit 훅 6개 — 임계값/어휘 리터럴 금지·gold 격리·생산검증 import 분리 등
.claude/agents/   에이전트 10종 정의(위임 라우팅) — 상세는 docs/design/AGENT_DESIGN.md
gold_label/       gold.json(gold50 사람 라벨)·vis100/(S0 자동 산출 + 라벨링 UI, 사람 gold 아직 없음)
docs/experiments/ 실험 설계 정본(experiment_design_v0.1.md)
```

## 실행 가능한 clip 세트 (`CLIP_SETS`, 2026-09-22 기준)

데이터셋(`/katech/datasets/visionary*`)이 세션 중 여러 차례 재구성됐다 — 세트별로 디스크 존재 여부가 다르다.

| 세트 | clip 수 | gold 채점 | 디스크 상태 |
|---|---|---|---|
| `gold50` | 50 | 가능(`gold.json`) | **0/50 — 원본 없음**(팀 재구성 진행 중) |
| `selected50` | 50 | 불가(회귀 기준선) | **0/50 — 원본 없음** |
| `vis100` | 100 | 불가(사람 gold 미작성) | **동작함**(100/100, S0·Track1 98/100 검증됨) |

```bash
./run.sh task_episode/run_pipeline.py --set vis100 --exp <실험명> --workers 4
./run.sh verification/report.py --exp <실험명>
```

## 실행

```bash
./run.sh <script.py> [args]        # venv 실행 + logs/latest.log
```

정식 test/lint는 없다(pre-commit 훅이 부분 대체). VLM 서버는 NVIDIA NIM(cosmos3-nano-reasoner) — `cosmos_dj`(8000, 평가-frozen, **무접촉**)와 replica 3개(8001~8003, 라운드로빈)로 구성.

## 현재 상태

문서는 3층 구조로 관리한다 — **현재 코드 실태 / 목표 설계(v0.4, 미구현) / 불변 규칙**. 자세한 내용과
운영 지침은 [`CLAUDE.md`](CLAUDE.md) 참조.

최근 작업(2026-09): 에이전트 위임 체계(10종) 구축 + C001 사이클(gold50 50클립) 완주·재현성 검증,
`outputs/episodes·labels`의 exp 네임스페이스 분리(사이클 간 덮어쓰기 방지), 데이터셋 재구성 대응
(`visionary` 경로 이관, native v1 map 스키마, pose 스키마, frame gallery 기반 VLM 입력 전환),
vis100(신규 100clip) 100/100 데모 실행, 실험 A(추론 경로) 중 A1 시퀀스 점수화 경로 원리 검증
(`common/scoring.py`). 근거와 수치는 [`decisions/DESIGN_LOG.md`](decisions/DESIGN_LOG.md) 최신
항목 참조.

**알려진 미해결 항목**: Track2 mp4 전용 경로(vis100 미대응) · 고속 차선변경 미검출(`common/events.py`
요레이트 게이트) · gold50/selected50 데이터 원본 부재 · 실험 A2/A4·B·C·D 미구현(A3·A1 원리검증만).
전체 목록은 `decisions/DESIGN_LOG.md`의 "파이프라인 현재 문제점 종합 목록" 항목 참조.

## 불변 규칙 요약

- **Frozen tagger**: fine-tuning 금지(순환성 차단). 평가용 복제본(port 8001)은 별도 taggers와 무접촉
- **앵커 비오염**: 전이 시점(`key_frame_t`)과 종방향 거동은 CAN 규칙 산출 — 모델 출력이 중간 경유로도 진입 금지
- **재현성**: seed 고정만으로는 불충분(배치 크기·프리픽스 캐시 히트에 따라 결과가 흔들림) — 매니페스트로 기록

전체 규칙은 [`CLAUDE.md`](CLAUDE.md) §3 참조.

---
> **구조 변경 시 이 README도 함께 갱신한다** — 단계/파일 대응, 제품 동작 상태, clip 세트 가용성이 바뀌면 위 표를 갱신할 것.
