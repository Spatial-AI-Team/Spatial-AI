# -*- coding: utf-8 -*-
"""점수화(scored) 경로 — 후보 문자열의 토큰별 로그확률을 chat completions logprobs로 수집.

실험 설계 정본(`docs/experiments/experiment_design_v0.1.md`) §A.1 "점수화 경로"의 실제 구현
(원리 검증용, arm 배선 아님). 이 서버(vLLM/NIM OpenAI 호환, `nvidia/cosmos3-nano-reasoner`)는
`/v1/completions`의 echo+prompt_logprobs를 지원하지 않고(멀티모달 입력이 completions
엔드포인트 자체에서 거부됨 — 별도 조사 확인), chat completions에도 임의 텍스트에 대한
"echo" 옵션이 없다. 따라서 실측으로 확인된 유일한 실현 경로를 쓴다:

  각 후보 문자열을 토크나이저로 토큰화 → 토큰을 하나씩 assistant 메시지에 프리필(누적)
  → extra_body={"continue_final_message": True, "add_generation_prompt": False}로
  "이어쓰기"(모델의 assistant 턴 시작 특수토큰을 다시 넣지 않고 프리필 뒤에 바로 이어붙임)
  지시 → logprobs=True, top_logprobs=<서버 상한>, max_tokens=1로 "다음 토큰" 분포를 받음
  → 그 top_logprobs 리스트에서 우리가 강제하려는 다음 후보 토큰을 찾아 logprob을 읽음
  → 이걸 후보 토큰 수만큼 반복해 누적.

이 방식은 2026-09-22 실측으로 확인됨(텍스트 전용 + 실제 vis100 클립 이미지 5장 포함 양쪽
모두 정상 응답, `logprobs.content[0].top_logprobs`에 원하는 토큰이 포함됨). 서버는
`top_logprobs<=20`만 허용(21 이상 요청 시 400 실측) — 이 20이라는 값은 서버 강제값이라
설계 변수가 아니지만, "찾는 토큰이 top-20 안에 없으면 어떻게 하는가"는 아래 fallback 정책
(코드 내 `_TOP_LOGPROBS_MAX` 및 truncated 플래그)으로 남긴다.

배치 순서(§A.7 요건 — 후보는 프롬프트 말미에만):
  이 함수는 media(영상/이미지)를 user 메시지에 먼저 넣고, 질의 텍스트를 그 뒤에 붙인다.
  후보 문자열은 그 다음 turn인 assistant 프리필로, 즉 가장 마지막에 온다. 현재 A3 경로
  (`task_episode/tag_v08.py`의 `_reason()`)는 텍스트를 media보다 앞에 두는 반대 순서를
  쓰는데, 그 판정 로직은 이 모듈이 건드리지 않는다 — 이 모듈은 완전히 새로운 독립 함수만
  제공한다.

길이 정규화: `mean_logprob = total_logprob / n_tokens`(토큰별 로그확률의 산술평균).
자연로그 영역의 산술평균은 확률 영역의 기하평균과 같다(exp(mean_logprob) = 후보 토큰별
확률의 기하평균) — 정본 §A.1 표의 "길이 정규화" 열(예: on_left 0.149의 제곱근 0.39)과
동일한 정의. 토큰 수가 다른 후보 간 비교 시 이 값을 쓴다(원시 `total_logprob` 합은 짧은
후보에 유리하게 편향됨).

이 모듈은 판정 로직(`tag_v08.py`, `classify073.py`)을 전혀 건드리지 않는다 — 신규 함수만
추가한다. 러너(`run_pipeline.py`)에 arm 스위치로 배선하지 않는다(별도 결정 사항).
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Optional

from config import MODEL

# 서버 실측 상한(2026-09-22, 21 이상 요청 시 400) — 설계 변수 아님, 서버 강제값.
_TOP_LOGPROBS_MAX = 20

_DEFAULT_TOKENIZER_DIR = Path(__file__).parent / "schema" / ".tokenizer_cache"

_tokenizer_cache = {}


def _get_tokenizer(tokenizer_dir: Optional[Path] = None):
    """로컬 토크나이저 캐시(Qwen2Tokenizer) 로드, 프로세스 내 재사용."""
    d = Path(tokenizer_dir) if tokenizer_dir else _DEFAULT_TOKENIZER_DIR
    key = str(d)
    if key not in _tokenizer_cache:
        if not (d / "tokenizer.json").exists():
            raise FileNotFoundError(
                f"토크나이저 캐시 없음: {d} — common/schema/scoring_probe.py 상단 docstring의 "
                f"추출 절차(docker exec <replica> cat ... > .tokenizer_cache/...) 먼저 실행")
        from transformers import AutoTokenizer
        _tokenizer_cache[key] = AutoTokenizer.from_pretrained(str(d))
    return _tokenizer_cache[key]


def _assemble_messages(media: list, query_text: str, system_text: Optional[str] = None) -> list:
    """[media...] 먼저, 질의 텍스트를 그 뒤(§A.7: 후보는 프롬프트 말미에만 — 여기선 아직
    assistant 프리필 이전 단계). system_text는 정적 블록(어휘·few-shot) 자리, 이 검증
    단계에서는 선택 사항."""
    messages = []
    if system_text:
        messages.append({"role": "system", "content": system_text})
    messages.append({"role": "user", "content": [*media, {"type": "text", "text": query_text}]})
    return messages


def score_candidate(client, media: list, query_text: str, candidate: str, field_key: str,
                     *, model: str = MODEL, top_logprobs: int = _TOP_LOGPROBS_MAX,
                     system_text: Optional[str] = None, assistant_prefix: Optional[str] = None,
                     tokenizer_dir: Optional[Path] = None, max_retries: int = 1) -> dict:
    """후보 문자열 하나를 토큰 단위로 프리필하며 로그확률을 누적.

    반환: {"candidate", "field", "n_tokens", "tokens":[{token,logprob,truncated}],
           "total_logprob", "mean_logprob", "truncated_any"}
    - total_logprob: 토큰별 logprob 합 (시퀀스 총 로그확률)
    - mean_logprob : total_logprob / n_tokens (길이 정규화, 토큰당 평균 로그확률 — 모듈
      docstring 참고. exp(mean_logprob) = 후보 토큰들의 기하평균 확률)
    - truncated_any: 어느 한 토큰이라도 top-{top_logprobs} 밖이라 정확값 대신 하한 근사를
      썼는지(무기록 fallback 금지 — 항상 이 필드로 노출)
    """
    top_logprobs = min(top_logprobs, _TOP_LOGPROBS_MAX)
    tok = _get_tokenizer(tokenizer_dir)
    ids = tok.encode(candidate, add_special_tokens=False)
    if not ids:
        return {"candidate": candidate, "field": field_key, "n_tokens": 0, "tokens": [],
                "total_logprob": 0.0, "mean_logprob": float("-inf"), "truncated_any": False,
                "error": "empty_token_sequence"}

    prefix = assistant_prefix if assistant_prefix is not None else f'{{"{field_key}": "'
    messages = _assemble_messages(media, query_text, system_text)

    per_token = []
    cum_ids: list = []
    for i, tid in enumerate(ids):
        assistant_text = prefix + tok.decode(cum_ids)
        call_messages = messages + [{"role": "assistant", "content": assistant_text}]
        target_tok = tok.convert_ids_to_tokens([tid])[0]

        resp = None
        last_err = None
        for attempt in range(max_retries + 1):
            try:
                resp = client.chat.completions.create(
                    model=model, temperature=0, max_tokens=1,
                    logprobs=True, top_logprobs=top_logprobs,
                    messages=call_messages,
                    extra_body={"continue_final_message": True, "add_generation_prompt": False})
                break
            except Exception as e:  # noqa: BLE001 — API 호출 예외 1회 재시도 후 전파(CLAUDE.md 규칙)
                last_err = e
                resp = None
        if resp is None:
            raise RuntimeError(
                f"score_candidate: API 호출 {max_retries + 1}회 실패(candidate={candidate!r}, "
                f"token_idx={i}) — {last_err}") from last_err

        choice_lp = resp.choices[0].logprobs.content[0]
        entry = next((t for t in choice_lp.top_logprobs if t.token == target_tok), None)
        if entry is not None:
            lp = entry.logprob
            truncated = False
        else:
            # 후보 토큰이 top-K 밖 — 정확값을 알 수 없으므로 리스트 내 최하위 로그확률을
            # 상한(그 토큰의 실제 확률은 이보다 작거나 같음)으로 대체하고 플래그를 남긴다
            # (CLAUDE.md 불변규칙4: 무기록 fallback 금지).
            lp = min(t.logprob for t in choice_lp.top_logprobs)
            truncated = True
        per_token.append({"token": target_tok, "logprob": lp, "truncated": truncated})
        cum_ids.append(tid)

    total_logprob = sum(t["logprob"] for t in per_token)
    n = len(per_token)
    mean_logprob = total_logprob / n if n else float("-inf")
    return {
        "candidate": candidate, "field": field_key, "n_tokens": n, "tokens": per_token,
        "total_logprob": total_logprob, "mean_logprob": mean_logprob,
        "truncated_any": any(t["truncated"] for t in per_token),
    }


def score_candidates(client, media: list, query_text: str, candidates: list, field_key: str,
                      **kwargs) -> dict:
    """후보 여러 개를 각각 `score_candidate`로 채점 후 재정규화 확률 + 엔트로피까지 부착.

    재정규화: p_i = exp(mean_logprob_i) / sum_j exp(mean_logprob_j) (기하평균 확률을 후보
    집합 내에서만 다시 정규화 — 정본 §A.1 표의 "재정규화" 개념과 동일).
    엔트로피: 위 재정규화 분포의 섀넌 엔트로피(자연로그, nats).
    """
    results = [score_candidate(client, media, query_text, c, field_key, **kwargs) for c in candidates]
    means = [r["mean_logprob"] for r in results]
    m = max(means) if means else 0.0
    exps = [math.exp(mv - m) for mv in means]  # overflow 방지용 max-shift
    z = sum(exps) or 1.0
    probs = [e / z for e in exps]
    entropy = -sum(p * math.log(p) for p in probs if p > 0)
    for r, p in zip(results, probs):
        r["renormalized_prob"] = p
    ranked = sorted(results, key=lambda r: r["renormalized_prob"], reverse=True)
    return {
        "field": field_key, "candidates": results,
        "top1": ranked[0]["candidate"] if ranked else None,
        "entropy_nats": entropy,
    }
