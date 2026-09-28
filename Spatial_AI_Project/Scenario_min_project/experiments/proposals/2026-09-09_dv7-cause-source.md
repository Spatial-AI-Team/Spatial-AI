# 제안 — DV-7(중간 산출물 외부화): `cause_source` 필드 추가

## 1. 관측
`reports/C001.md` §7 (물리 게이트) + `experiments/results/C001/gate.json`:

> segment 60개 중 any_fail 33개. `reachable` 검사 단독으로 fail=32, undetermined=28 (pass 없음, 설계상 정상).

`gate.json`을 세그먼트 단위로 열어보면 fail=32의 실제 사유는 전부 동일하다: `cause="agent"`로 확정된 세그먼트인데 `critical_components` 어디에도 `ref.distance_m`(GT 거리 계측)이 없다. `task_episode/tag_v08.py:270-277`를 보면 `cause` 산출 경로가 둘로 갈린다 —

```python
if in_path:            # GT(3DOD) in-path 객체 존재 → cause="agent" (물리적으로 검증 가능)
    cause = "agent"
else:                   # GT 없음 → 모델 자유분류 v.get("cause") 채택 (검증 불가능할 수 있음)
    cause = v.get("cause") or "other"
```

두 경로 모두 최종 산출물엔 `cause: "agent"`로 **동일하게** 남는다 — 어느 경로로 나왔는지가 기록되지 않는다. `verification/score_gold.py` 결과(`recall=0.821`, 기존 문서 recall 0.81과 정합적으로 재현됨 — 채점 로직 자체는 신뢰 가능)와 별개로, 이 gate 결과는 "GT가 뒷받침하지 않는 agent 귀속이 32/60(53%)"라는, 지금까지 어떤 리포트에도 없던 신규 관측이다.

## 2. 대상 DV
**DV-7 (중간 산출물 외부화)** 정확히 하나. — `pipeline_design_guide_v0.4.md` §"설정으로 열어둘 것" 표의 "중간 산출물 외부화: 2-pass / 단일 pass" 축과 같은 축(S3 판정의 근거를 기록으로 남기느냐)이며, 어휘·임계·프롬프트 축은 건드리지 않는다.

## 3. 현재값 → 제안값
- 현재: `cause` 문자열 1개만 기록(어느 경로로 확정됐는지 소실)
- 제안: 레코드에 `cause_source ∈ {gt_confirmed, model_reported}` 필드 추가 — `in_path` truthy 분기면 `gt_confirmed`, else 분기면 `model_reported`. **판정 로직·enum·임계는 변경하지 않는다** — 이미 계산된 분기 결과를 기록만 하는 변경(무기록 fallback 금지 원칙과 동일 성격의 보완)

## 4. 가설
`cause_source`가 기록되면 물리 게이트의 `reachable` 검사가 `cause_source=model_reported`인 세그먼트를 "GT 근거 없음 → undetermined(검증 불가 명시)"로, `gt_confirmed`인 세그먼트만 실제 거리 임계로 `fail`/`(거리 충족)`을 나눠 판정할 수 있게 된다. 즉 지금 32건이 뭉뚱그려 "fail"로 잡히는 것 중 상당수가 "애초에 검증 대상이 아닌 model_reported 귀속"으로 재분류되어, verifier가 재라벨 우선순위를 gt_confirmed 실패 건에 집중할 수 있다.

## 5. 예상 부작용
- `model_reported` 비중이 예상보다 커서(예: 32/60 전부) 물리 게이트가 사실상 무력화(대부분 undetermined로 빠짐)될 수 있다 — 이 경우 게이트의 실효 커버리지가 오히려 줄어든 것으로 드러나 "게이트가 뭘 검증하고 있는지"를 재점검해야 한다
- `model_reported` 귀속 자체가 모델의 실질 정확도를 반영할 수도 있어(GT가 놓친 진짜 agent 원인), 이를 전부 "검증 불가"로 미룰 경우 그 세그먼트들의 품질 신호가 사라진다 — dataset-curator의 gold 역검증에서 `model_reported`·`gt_confirmed` 두 그룹을 층화해 별도 정확도로 봐야 함

## 6. 반증 조건
층화 후 재실행한 gate.json에서 `cause_source=gt_confirmed`인 세그먼트의 `reachable` fail 비율이 현재 전체 fail 비율(32/60=53%)과 **유의하게 다르지 않다면**(즉 gt_confirmed 여부가 fail과 무관하다면) 이 가설은 틀렸다 — 원인은 다른 데 있다(예: `_gt_ref()`가 항상 `distance_m`을 채우지 못하는 별도 버그).

## 7. 비용
- 코드 변경: `tag_v08.py` cause 산출 블록에 1줄 추가(필드 부착) — 재처리 필요. gold 50 전체 재실행(현재 costs: wall-clock ~8분, workers=4)
- `verification/gate.py`의 `reachable` 검사 로직도 `cause_source` 분기를 반영하도록 소폭 확장 필요(같은 변경축 내 — 게이트가 새 필드를 읽는 것뿐, 판정 임계는 안 바꿈)

## 8. 선행 확인
`decisions/DESIGN_LOG.md` 전체에 `cause_source`·`gt_confirmed`·`model_reported` 관련 이전 시도나 기각 이력 없음(grep 확인, 신규 관측). `disclosure.py`의 `S2_CONDITIONING` 필드와는 다른 축(그건 "S2 입력에 규칙값이 섞였는가", 이건 "S3 cause 값의 근거가 GT인가 모델인가") — 혼동하지 않도록 이름을 분리했다.
