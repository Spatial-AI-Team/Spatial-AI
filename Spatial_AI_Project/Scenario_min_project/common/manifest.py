# -*- coding: utf-8 -*-
"""재현성 매니페스트 5군 — `tag_vocab_v0.4.json` `versioning.reproducibility_params`,
CLAUDE.md "재현성 매니페스트 5군" 표. DESIGN_LOG 미결 항목("기록하는 코드가 없다")의 구현.

`common/disclosure.py`(공개의무 4필드: causal_judgment_scope 등)와는 다른 계층이다.
disclosure.stamp()는 **레코드마다** 붙는 정책 사실이고, 이 모듈은 **실험(run) 단위로
1회** 기록하는 실행 환경 스냅샷이다. disclosure의 4필드는 `input` 군에 흡수한다.

원칙(무기록 fallback 금지): 값을 얻을 수 없는 항목은 추정하지 않고
"unavailable:<사유>" 문자열을 넣어 그 사실 자체를 기록한다. 조용히 생략하지 않는다.

주의(guide §8.3): seed 고정만으로는 재현이 보장되지 않는다 — 서빙 배치 크기·프리픽스
캐시 히트 여부에 따라 부동소수점 합산 순서가 달라질 수 있다. 이 매니페스트는 그 사실을
가리지 않고, 동일 조건 재실행 시 해시 비교로 드러나게 하는 것이 목적이다.
"""
import hashlib
import json

import config
import disclosure


def _probe_model_info(base_url: str, timeout: float = 2.0) -> dict:
    """/v1/models 로 얻을 수 있는 것만. checkpoint_hash·dtype·tp 는 NIM이 노출하지 않음."""
    import urllib.request
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/models", timeout=timeout) as r:
            data = json.loads(r.read())
        d0 = (data.get("data") or [{}])[0]
        return {"served_model_id": d0.get("id"), "served_model_root": d0.get("root"),
                "max_model_len": d0.get("max_model_len")}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def build(*, seed=None, static_prefix_hash: str = None, exemplar_bucket_id: str = "none",
          frame_count: int = None, frame_rate: int = None, resolution: int = None,
          views_used=None, gt_version: str = "visionary-nvidia", vocab_version: str = "v0.4",
          threshold_set_id: str = "unversioned-2026-09", inference_path: str = "generate",
          replicas: list = None) -> dict:
    """실험(run) 1회당 매니페스트. 값을 못 채우면 "unavailable:<사유>"로 명시."""
    probe = _probe_model_info(config.BASE_URL)
    served_id = probe.get("served_model_id", "unavailable:models_endpoint_probe_failed")

    model = {
        "checkpoint_hash": "unavailable:NIM 컨테이너가 /v1/models로 체크섬 미노출 (frozen, blake3 무접촉)",
        "tokenizer_version": "unavailable:서빙 API로 미노출 — common/schema/scoring_probe.py 토크나이저 인자로 별도 확인 필요",
        "dtype": "unavailable:서빙 API로 미노출",
        "tensor_parallel_size": "unavailable:서빙 API로 미노출",
        "served_model_id": served_id,
        "served_model_root": probe.get("served_model_root"),
        "max_model_len": probe.get("max_model_len"),
    }
    serving = {
        "max_concurrent_seqs": "unavailable:서빙 API로 미노출",
        "prefix_cache_enabled": "unavailable:서빙 API로 미노출",
        "cuda_graph_enabled": "unavailable:서빙 API로 미노출",
        "serving_framework_version": "unavailable:서빙 API로 미노출",
        "replicas": replicas or [],
        "note": "8000(cosmos2, 평가 대상 밖) 무접촉. 8001-8004 라운드로빈만 사용",
    }
    decoding = {
        "seed": seed,
        "temperature": config.TEMPERATURE,
        "top_p": "unavailable:config.py 미노출(서버 기본값 사용)",
        "top_k": "unavailable:config.py 미노출(서버 기본값 사용)",
        "max_output_tokens": config.MAX_TOKENS,
        "inference_path": inference_path,
        "note": "seed 고정만으로 재현 보장 안 됨 — 배치 크기·prefix cache 히트에 따라 부동소수점 "
                "합산 순서가 달라질 수 있음(guide §8.3)",
    }
    inp = {
        "static_prefix_hash": static_prefix_hash or "unavailable:정적 프리픽스 미고정(프롬프트 조립이 클립마다 GT 힌트로 가변)",
        "exemplar_bucket_id": exemplar_bucket_id,
        "frame_count": frame_count,
        "frame_rate": frame_rate or config.SEND_FPS,
        "resolution": resolution or config.WINDOW_MAX_SIDE,
        "views_used": views_used or ["camera_front_wide_120fov"],
        "causal_judgment_scope": disclosure.CAUSAL_JUDGMENT_SCOPE,
        "s2_conditioning": disclosure.S2_CONDITIONING,
    }
    data = {
        "gt_version": gt_version,
        "vocab_version": vocab_version,
        "threshold_set_id": threshold_set_id,
        "placement_policy_version": "unavailable:selection 정책 버전 태그 미도입",
    }
    return {"model": model, "serving": serving, "decoding": decoding, "input": inp, "data": data}


def content_hash(obj) -> str:
    """산출 JSON 비교용. sort_keys=True로 키 순서 무관 해시(repo 전체에 없던 관행 — 여기서 시작)."""
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
