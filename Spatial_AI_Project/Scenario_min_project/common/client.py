# -*- coding: utf-8 -*-
"""VLM 클라이언트 풀 (활성 유틸). Task: common.

BASE_URLS(8001–8004) 중 헬스체크 통과분으로 OpenAI 호환 클라이언트 풀 생성 → 라운드로빈.
(구 tagger.build_client_pool 에서 추출 — legacy 의존 없이 활성 파이프라인이 쓰도록 공용화.)
"""
from openai import OpenAI

from config import API_KEY, BASE_URL, BASE_URLS


def build_client_pool(timeout: float = 2.0) -> list:
    """살아있는 엔드포인트로 클라이언트 풀. 미기동 복제본은 자동 제외."""
    import urllib.request
    alive = []
    for url in BASE_URLS:
        try:
            urllib.request.urlopen(url.rstrip("/") + "/models", timeout=timeout)
            alive.append(url)
        except Exception:
            pass
    if not alive:
        alive = [BASE_URL]
    return [OpenAI(base_url=u, api_key=API_KEY) for u in alive]


# --- 멀티모달 메시지 조립 (2026-09-11: 프레임 갤러리 clip 대응 신설) ---------
# 기존 호출부(tag_v08.py 등)는 `{"type": "video_url", "video_url": {"url": uri}}` 1개짜리
# content part를 인라인으로 만들어 왔다. 신규 visionary-nvidia 100 clip(mp4 없음, jpg 갤러리)
# 은 video_url을 만들 uri 자체가 없으므로 image_url 여러 장을 배열로 보내야 한다. 아래 두
# 헬퍼는 그 두 경로를 함수 하나로 감싸 호출부가 "어느 소스인지"만 분기하고 content 배열
# 조립 형식은 신경 쓰지 않게 한다. 기존 video_url 경로는 그대로 남기고(다른 활성
# 데이터셋이 mp4를 쓸 경우 대비), 신규 image 시퀀스 경로를 병행 지원(택일 아님).


def video_content(uri: str) -> list:
    """단일 video_url data URI → message content part 배열(기존 경로 그대로)."""
    return [{"type": "video_url", "video_url": {"url": uri}}]


def image_sequence_content(uris: list) -> list:
    """image_url data URI 여러 장 → message content part 배열(시간순 그대로, 라벨 텍스트
    파트 없음 — 순서 자체가 시간축이라는 것은 호출부의 지시문(text part)에서 설명해야 함)."""
    return [{"type": "image_url", "image_url": {"url": u}} for u in uris]


def visual_content(mode: str, uri_or_uris) -> list:
    """mode="video" → video_content(단일 uri) / mode="images" → image_sequence_content(uri 리스트).
    호출부가 소스(mp4 vs 프레임갤러리) 판별 후 이 함수 하나로 위임하도록 하는 진입점."""
    if mode == "video":
        return video_content(uri_or_uris)
    if mode == "images":
        return image_sequence_content(uri_or_uris)
    raise ValueError(f"알 수 없는 visual mode: {mode}")
