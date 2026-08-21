"""
マッチング申請の状態を管理するモジュール。

「同じ相手への重複申請を防ぐ」ための仕組みで、
(申請した人, 申請された人) のペアごとに「承認待ち」状態を記録します。

シンプルにするためメモリ上（プロセス内の辞書）で管理しています。
Railwayの再起動やデプロイでプロセスが再起動すると、承認待ちの情報は
リセットされます（＝再起動後は改めて申請し直せます）。
より厳密に永続化したい場合はSQLite等への置き換えを検討してください。
"""

from __future__ import annotations

import time
import threading
import uuid

REQUEST_EXPIRY_SECONDS = 24 * 60 * 60  # 24時間経っても未回答なら申請切れとして扱う

_lock = threading.Lock()
# request_id -> {requester_id, target_id, created_at}
_requests: dict[str, dict] = {}


def _purge_expired_locked():
    now = time.time()
    expired_ids = [
        rid
        for rid, req in _requests.items()
        if now - req["created_at"] > REQUEST_EXPIRY_SECONDS
    ]
    for rid in expired_ids:
        del _requests[rid]


def has_pending_request(requester_id: str, target_id: str) -> bool:
    with _lock:
        _purge_expired_locked()
        return any(
            req["requester_id"] == str(requester_id)
            and req["target_id"] == str(target_id)
            for req in _requests.values()
        )


def create_request(requester_id: str, target_id: str) -> str | None:
    """
    新規のマッチング申請を作成する。
    同じ相手への申請がすでに承認待ちの場合は None を返す（重複防止）。
    成功した場合は request_id を返す。
    """
    with _lock:
        _purge_expired_locked()

        for req in _requests.values():
            if (
                req["requester_id"] == str(requester_id)
                and req["target_id"] == str(target_id)
            ):
                return None

        request_id = uuid.uuid4().hex
        _requests[request_id] = {
            "requester_id": str(requester_id),
            "target_id": str(target_id),
            "created_at": time.time(),
        }
        return request_id


def get_request(request_id: str) -> dict | None:
    with _lock:
        _purge_expired_locked()
        return _requests.get(request_id)


def resolve_request(request_id: str) -> dict | None:
    """承認・拒否のいずれかで申請を確定させ、内容を返しつつ辞書から削除する。"""
    with _lock:
        return _requests.pop(request_id, None)


def pending_targets_for(requester_id: str) -> set[str]:
    """指定した申請者が、現在承認待ち中の相手IDの集合を返す（Web側の表示用）。"""
    with _lock:
        _purge_expired_locked()
        return {
            req["target_id"]
            for req in _requests.values()
            if req["requester_id"] == str(requester_id)
        }
