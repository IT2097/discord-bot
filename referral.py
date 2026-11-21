"""
リジェコミュニティの紹介制度（Stripe 連携）。

【流れ】
1. 会員が Discord の「紹介リンク」ボタンから、自分の紹介コードと紹介リンクを受け取る
   （紹介リンク：https://resense-inc.com/reje/?ref=コード）。
   報酬を会費から差し引くために、Stripe に登録したメールアドレスも入力してもらう。
2. 紹介リンクから来た人がサイトの料金表で申し込むと、コードが Stripe の
   client_reference_id として決済に付く（サイト側の reje/index.html で設定）。
3. Stripe から届く知らせ（Webhook）を受けて、スプレッドシートに記録する。
   ・customer.subscription.*   → 「契約者一覧」：誰がどのプランか
   ・checkout.session.completed → 「紹介記録」：誰の紹介で入ったか
   ・invoice.paid              → 「紹介報酬」：紹介された人が払った月ごとに1行（付与は翌月末）
4. 定期処理で、付与日（支払った月の翌月末）が来た報酬を、紹介者の Stripe の残高に
   クレジットとして入れる（＝次回の会費から自動で差し引かれる）。

【報酬のルール（紹介制度規約より）】
・紹介された人が一般プラン（2,200円）なら月500円、チャンネル作成プラン（4,400円）なら月1,000円
・報酬を受け取れるのは、紹介された人が支払った時点で、紹介者がチャンネル作成プランの会員のときだけ
・付与する時点で紹介者が退会していたら付与しない
・紹介者本人による入会は対象外

【必要な環境変数】
STRIPE_API_KEY         … Stripe の秘密キー（制限付きキー推奨。顧客・サブスクリプション・請求書・Checkout の読み取り、
                          顧客の残高（Customer balance）とサブスクリプションの書き込み）
STRIPE_WEBHOOK_SECRET  … Webhook の署名シークレット（whsec_...）
（任意）STRIPE_PRICE_GENERAL / STRIPE_PRICE_CREATOR … プランの価格ID。未設定なら金額（2,200円／4,400円）で見分ける
（任意）REFERRAL_LINK_BASE … 紹介リンクの元のURL（標準：https://resense-inc.com/reje/）
"""

from __future__ import annotations

import calendar
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

import gspread
from google.oauth2.service_account import Credentials

try:
    import stripe
except ImportError:  # requirements.txt に入れているが、念のため
    stripe = None

JST = timezone(timedelta(hours=9))

STRIPE_API_KEY = os.getenv("STRIPE_API_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
PRICE_GENERAL = os.getenv("STRIPE_PRICE_GENERAL", "")
PRICE_CREATOR = os.getenv("STRIPE_PRICE_CREATOR", "")
REFERRAL_LINK_BASE = os.getenv("REFERRAL_LINK_BASE", "https://resense-inc.com/reje/")

PLAN_GENERAL = "一般プラン"
PLAN_CREATOR = "チャンネル作成プラン"
REWARD_BY_PLAN = {PLAN_GENERAL: 500, PLAN_CREATOR: 1000}
_PLAN_BY_AMOUNT = {2200: PLAN_GENERAL, 4400: PLAN_CREATOR}

STATUS_SCHEDULED = "予定"
STATUS_GRANTED = "付与済み"
STATUS_EXCLUDED = "対象外"
STATUS_ERROR = "エラー"

SHEET_CODES = "紹介コード"
SHEET_REFERRALS = "紹介記録"
SHEET_REWARDS = "紹介報酬"
SHEET_SUBSCRIBERS = "契約者一覧"

_HEADERS = {
    SHEET_CODES: ["紹介コード", "DiscordID", "名前", "Stripeのメールアドレス", "発行日時", "更新日時"],
    SHEET_REFERRALS: [
        "入会日時", "紹介コード", "紹介者", "紹介者DiscordID", "入会した人", "入会した人のメール",
        "プラン", "StripeサブスクリプションID", "Stripe顧客ID",
    ],
    SHEET_REWARDS: [
        "支払日", "紹介コード", "紹介者", "入会した人", "プラン", "報酬額", "付与予定日", "状態",
        "付与日", "メモ", "Stripe請求書ID", "StripeサブスクリプションID",
    ],
    SHEET_SUBSCRIBERS: [
        "Stripe顧客ID", "名前", "メールアドレス", "プラン", "状態", "開始日", "解約日",
        "紹介コード", "StripeサブスクリプションID", "更新日時",
    ],
}

_CODE_CHARS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # 見間違えやすい 0/O・1/I/L は使わない
_CODE_PATTERN = re.compile(r"^[A-Z0-9]{4,20}$")

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_lock = threading.RLock()  # Webhook（Flaskのスレッド）と定期処理が同時にシートを書き換えないように
_sheets: dict[str, gspread.Worksheet] = {}


class ReferralError(Exception):
    """会員にそのまま見せてよいエラー。"""


def is_configured() -> bool:
    return bool(stripe and STRIPE_API_KEY)


def _stripe():
    if not is_configured():
        raise ReferralError("Stripe の設定（STRIPE_API_KEY）がまだありません。運営に連絡してください。")
    stripe.api_key = STRIPE_API_KEY
    return stripe


def _now_text() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")


def _date_text(ts: int | float | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, JST).strftime("%Y-%m-%d")


def _get(obj, *keys, default=None):
    """Stripe のオブジェクト（辞書のように扱える）から、入れ子のキーを安全に取り出す。"""
    for key in keys:
        if obj is None:
            return default
        try:
            obj = obj[key]
        except (KeyError, IndexError, TypeError):
            return default
    return default if obj is None else obj


# ---------------------------------------------------------------------------
# スプレッドシート
# ---------------------------------------------------------------------------

def _sheet(name: str) -> gspread.Worksheet:
    with _lock:
        if name in _sheets:
            return _sheets[name]
        raw_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
        spreadsheet_id = os.getenv("SPREADSHEET_ID")
        if not raw_json or not spreadsheet_id:
            raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON / SPREADSHEET_ID が設定されていません。")
        creds = Credentials.from_service_account_info(json.loads(raw_json, strict=False), scopes=_SCOPES)
        spreadsheet = gspread.authorize(creds).open_by_key(spreadsheet_id)
        try:
            sheet = spreadsheet.worksheet(name)
        except gspread.exceptions.WorksheetNotFound:
            headers = _HEADERS[name]
            sheet = spreadsheet.add_worksheet(title=name, rows=1000, cols=len(headers))
            sheet.append_row(headers, value_input_option="RAW")
        _sheets[name] = sheet
        return sheet


def _rows(name: str) -> list[dict]:
    """シートの全行を {見出し: 値, "_row": 行番号} の形で返す。"""
    values = _sheet(name).get_all_values()
    headers = _HEADERS[name]
    rows = []
    for number, row in enumerate(values[1:], start=2):
        row = list(row) + [""] * (len(headers) - len(row))
        item = {h: str(row[i]).strip() for i, h in enumerate(headers)}
        item["_row"] = number
        rows.append(item)
    return rows


def _append(name: str, item: dict):
    _sheet(name).append_row([str(item.get(h, "")) for h in _HEADERS[name]], value_input_option="RAW", table_range="A1")


def _update(name: str, row_number: int, item: dict):
    headers = _HEADERS[name]
    last_col = gspread.utils.rowcol_to_a1(row_number, len(headers))
    _sheet(name).update(
        range_name=f"A{row_number}:{last_col}",
        values=[[str(item.get(h, "")) for h in headers]],
        value_input_option="RAW",
    )


# ---------------------------------------------------------------------------
# 紹介コード（Discord のボタンから使う）
# ---------------------------------------------------------------------------

def referral_link(code: str) -> str:
    sep = "&" if "?" in REFERRAL_LINK_BASE else "?"
    return f"{REFERRAL_LINK_BASE}{sep}ref={code}"


def get_code_for_member(discord_id: str) -> dict | None:
    with _lock:
        return next((r for r in _rows(SHEET_CODES) if r["DiscordID"] == str(discord_id)), None)


def _find_code(code: str) -> dict | None:
    code = (code or "").strip().upper()
    if not _CODE_PATTERN.match(code):
        return None
    return next((r for r in _rows(SHEET_CODES) if r["紹介コード"] == code), None)


def issue_code(discord_id: str, name: str, email: str) -> dict:
    """紹介コードを発行する（発行済みなら、名前とメールアドレスだけ更新して同じコードを返す）。"""
    email = (email or "").strip()
    if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise ReferralError("メールアドレスの形が正しくありません。もう一度入力してください。")
    with _lock:
        rows = _rows(SHEET_CODES)
        mine = next((r for r in rows if r["DiscordID"] == str(discord_id)), None)
        if mine is not None:
            mine.update({"名前": name, "Stripeのメールアドレス": email, "更新日時": _now_text()})
            _update(SHEET_CODES, mine["_row"], mine)
            return mine
        used = {r["紹介コード"] for r in rows}
        while True:
            code = "".join(secrets.choice(_CODE_CHARS) for _ in range(6))
            if code not in used:
                break
        item = {
            "紹介コード": code, "DiscordID": str(discord_id), "名前": name,
            "Stripeのメールアドレス": email, "発行日時": _now_text(), "更新日時": _now_text(),
        }
        _append(SHEET_CODES, item)
        return item


# ---------------------------------------------------------------------------
# Stripe の情報の読み取り
# ---------------------------------------------------------------------------

def _plan_of_price(price) -> str:
    price_id = _get(price, "id", default="")
    if PRICE_CREATOR and price_id == PRICE_CREATOR:
        return PLAN_CREATOR
    if PRICE_GENERAL and price_id == PRICE_GENERAL:
        return PLAN_GENERAL
    return _PLAN_BY_AMOUNT.get(_get(price, "unit_amount", default=0), "")


def _plan_of_subscription(subscription) -> str:
    for item in _get(subscription, "items", "data", default=[]) or []:
        plan = _plan_of_price(_get(item, "price"))
        if plan:
            return plan
    return "不明"


def _customer(customer_id: str):
    return _stripe().Customer.retrieve(customer_id)


def _active_subscription_of(customer_id: str):
    """その顧客の続いているサブスクリプション（無ければ None）。"""
    subs = _stripe().Subscription.list(customer=customer_id, status="all", limit=20)
    alive = [s for s in subs.auto_paging_iter() if _get(s, "status") in ("active", "trialing", "past_due")]
    if not alive:
        return None
    # チャンネル作成プランを優先する
    alive.sort(key=lambda s: _plan_of_subscription(s) != PLAN_CREATOR)
    return alive[0]


def _referrer_customer(code_row: dict):
    """紹介者の Stripe 顧客と、続いているサブスクリプション（見つからなければ None, None）。"""
    email = code_row.get("Stripeのメールアドレス", "")
    if not email:
        return None, None
    customers = _stripe().Customer.list(email=email, limit=10)
    best = (None, None)
    for customer in customers.auto_paging_iter():
        sub = _active_subscription_of(customer["id"])
        if sub is not None:
            if _plan_of_subscription(sub) == PLAN_CREATOR:
                return customer, sub
            if best[0] is None:
                best = (customer, sub)
        elif best[0] is None:
            best = (customer, None)
    return best


def _subscription_id_of_invoice(invoice) -> str:
    # API のバージョンによって、請求書のサブスクリプションIDの場所が違う
    return (
        _get(invoice, "subscription", default="")
        or _get(invoice, "parent", "subscription_details", "subscription", default="")
        or ""
    )


def _referral_code_of_subscription(subscription) -> str:
    """サブスクリプションに付いた紹介コード（メタデータ → 申し込み時の Checkout の client_reference_id の順に探す）。"""
    code = _get(subscription, "metadata", "referral_code", default="")
    if code:
        return code
    sessions = _stripe().checkout.Session.list(subscription=subscription["id"], limit=1)
    for session in sessions.data:
        code = (_get(session, "client_reference_id", default="") or "").strip().upper()
        if code and _find_code(code) is not None:
            try:  # 次からすぐ分かるように、サブスクリプションにも書いておく
                _stripe().Subscription.modify(subscription["id"], metadata={"referral_code": code})
            except Exception as error:  # noqa: BLE001 - 書けなくても Checkout から毎回探せる
                print(f"サブスクリプションに紹介コードを書けませんでした：{error}")
            return code
    return ""


# ---------------------------------------------------------------------------
# Webhook（Flask から呼ぶ）
# ---------------------------------------------------------------------------

def handle_webhook(payload: bytes, signature: str) -> tuple[str, int]:
    """Stripe からの知らせを処理する。戻り値は (本文, HTTPステータス)。"""
    if not is_configured() or not STRIPE_WEBHOOK_SECRET:
        return "stripe is not configured", 503
    try:
        event = _stripe().Webhook.construct_event(payload, signature, STRIPE_WEBHOOK_SECRET)
    except Exception as error:  # noqa: BLE001 - 署名が合わないものは受け付けない
        print(f"Stripe Webhook の署名を確認できませんでした：{error}")
        return "invalid signature", 400

    kind = event["type"]
    obj = event["data"]["object"]
    try:
        with _lock:
            if kind.startswith("customer.subscription."):
                _record_subscription(obj)
            elif kind == "checkout.session.completed":
                _record_checkout(obj)
            elif kind == "invoice.paid":
                _record_invoice(obj)
    except Exception as error:  # noqa: BLE001 - 500 を返すと Stripe が時間をおいて送り直してくれる
        import traceback

        traceback.print_exception(type(error), error, error.__traceback__)
        return "error", 500
    return "ok", 200


def _record_subscription(subscription):
    """「契約者一覧」を更新する（サブスクリプション1つにつき1行）。"""
    subscription = _stripe().Subscription.retrieve(subscription["id"])  # 最新の状態を取り直す
    customer = _customer(subscription["customer"])
    ended = _get(subscription, "ended_at") or _get(subscription, "canceled_at")
    item = {
        "Stripe顧客ID": customer["id"],
        "名前": _get(customer, "name", default=""),
        "メールアドレス": _get(customer, "email", default=""),
        "プラン": _plan_of_subscription(subscription),
        "状態": {
            "active": "契約中", "trialing": "お試し中", "past_due": "支払い遅れ", "unpaid": "未払い",
            "canceled": "解約", "incomplete": "手続き中", "incomplete_expired": "手続き期限切れ", "paused": "一時停止",
        }.get(_get(subscription, "status", default=""), _get(subscription, "status", default="")),
        "開始日": _date_text(_get(subscription, "start_date")),
        "解約日": _date_text(ended) if _get(subscription, "status") == "canceled" else "",
        "紹介コード": _get(subscription, "metadata", "referral_code", default=""),
        "StripeサブスクリプションID": subscription["id"],
        "更新日時": _now_text(),
    }
    existing = next((r for r in _rows(SHEET_SUBSCRIBERS) if r["StripeサブスクリプションID"] == subscription["id"]), None)
    if existing is not None:
        if not item["紹介コード"]:
            item["紹介コード"] = existing["紹介コード"]
        _update(SHEET_SUBSCRIBERS, existing["_row"], item)
    else:
        _append(SHEET_SUBSCRIBERS, item)


def _record_checkout(session):
    """紹介リンクから申し込まれたら「紹介記録」に1行追加する。"""
    code = (_get(session, "client_reference_id", default="") or "").strip().upper()
    subscription_id = _get(session, "subscription", default="")
    if not code or not subscription_id:
        return
    code_row = _find_code(code)
    if code_row is None:
        print(f"Stripe の申し込みに、登録されていない紹介コード「{code}」が付いていました。")
        return
    if any(r["StripeサブスクリプションID"] == subscription_id for r in _rows(SHEET_REFERRALS)):
        return  # 同じ知らせが2回届いた
    subscription = _stripe().Subscription.retrieve(subscription_id)
    try:
        _stripe().Subscription.modify(subscription_id, metadata={"referral_code": code})
    except Exception as error:  # noqa: BLE001
        print(f"サブスクリプションに紹介コードを書けませんでした：{error}")
    customer = _customer(subscription["customer"])
    _append(SHEET_REFERRALS, {
        "入会日時": _now_text(),
        "紹介コード": code,
        "紹介者": code_row["名前"],
        "紹介者DiscordID": code_row["DiscordID"],
        "入会した人": _get(customer, "name", default=""),
        "入会した人のメール": _get(customer, "email", default=""),
        "プラン": _plan_of_subscription(subscription),
        "StripeサブスクリプションID": subscription_id,
        "Stripe顧客ID": customer["id"],
    })
    # 契約者一覧にも紹介コードを反映する
    for row in _rows(SHEET_SUBSCRIBERS):
        if row["StripeサブスクリプションID"] == subscription_id and row["紹介コード"] != code:
            row["紹介コード"] = code
            _update(SHEET_SUBSCRIBERS, row["_row"], row)


def _end_of_next_month(ts: int) -> str:
    paid = datetime.fromtimestamp(ts, JST)
    year, month = (paid.year + 1, 1) if paid.month == 12 else (paid.year, paid.month + 1)
    return f"{year:04d}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}"


def _record_invoice(invoice):
    """紹介で入った人の支払いがあったら「紹介報酬」に1行追加する（付与は翌月末）。"""
    if (_get(invoice, "amount_paid", default=0) or 0) <= 0:
        return
    subscription_id = _subscription_id_of_invoice(invoice)
    if not subscription_id:
        return
    if any(r["Stripe請求書ID"] == invoice["id"] for r in _rows(SHEET_REWARDS)):
        return  # 同じ知らせが2回届いた
    subscription = _stripe().Subscription.retrieve(subscription_id)
    code = _referral_code_of_subscription(subscription)
    if not code:
        return  # 紹介ではない入会
    code_row = _find_code(code)
    if code_row is None:
        return

    plan = _plan_of_subscription(subscription)
    customer = _customer(subscription["customer"])
    paid_at = _get(invoice, "status_transitions", "paid_at") or _get(invoice, "created") or time.time()
    item = {
        "支払日": _date_text(paid_at),
        "紹介コード": code,
        "紹介者": code_row["名前"],
        "入会した人": _get(customer, "name", default="") or _get(customer, "email", default=""),
        "プラン": plan,
        "報酬額": REWARD_BY_PLAN.get(plan, 0),
        "付与予定日": _end_of_next_month(int(paid_at)),
        "状態": STATUS_SCHEDULED,
        "付与日": "",
        "メモ": "",
        "Stripe請求書ID": invoice["id"],
        "StripeサブスクリプションID": subscription_id,
    }

    # 支払った時点で、紹介者がチャンネル作成プランか確かめる（規約：一般プランの紹介者には報酬なし）
    referrer, referrer_sub = _referrer_customer(code_row)
    if not item["報酬額"]:
        item.update({"状態": STATUS_EXCLUDED, "メモ": f"プランを判別できません（{plan}）"})
    elif referrer is not None and referrer["id"] == customer["id"]:
        item.update({"状態": STATUS_EXCLUDED, "メモ": "紹介者本人による入会"})
    elif referrer is None:
        # メールアドレスの間違いなどもあり得るので、付与日に改めて確かめる
        item["メモ"] = "紹介者のStripeの契約が見つかりません（付与日に再確認）"
    elif referrer_sub is None or _plan_of_subscription(referrer_sub) != PLAN_CREATOR:
        item.update({"状態": STATUS_EXCLUDED, "メモ": "紹介者がチャンネル作成プランではありません"})
    _append(SHEET_REWARDS, item)


# ---------------------------------------------------------------------------
# 報酬の付与（定期処理・管理者コマンドから呼ぶ）
# ---------------------------------------------------------------------------

def grant_due_rewards() -> dict:
    """付与予定日が来た報酬を、紹介者の Stripe の残高にクレジットとして入れる。"""
    result = {"granted": 0, "amount": 0, "excluded": 0, "errors": []}
    today = datetime.now(JST).strftime("%Y-%m-%d")
    with _lock:
        codes = {r["紹介コード"]: r for r in _rows(SHEET_CODES)}
        for row in _rows(SHEET_REWARDS):
            if row["状態"] != STATUS_SCHEDULED or not row["付与予定日"] or row["付与予定日"] > today:
                continue
            code_row = codes.get(row["紹介コード"])
            try:
                referrer, referrer_sub = _referrer_customer(code_row) if code_row else (None, None)
                if referrer is None or referrer_sub is None:
                    row.update({"状態": STATUS_EXCLUDED, "メモ": "付与日に紹介者の契約が見つかりません（退会・メールアドレス違い）"})
                    result["excluded"] += 1
                elif "紹介者のStripeの契約が見つかりません" in row["メモ"] and _plan_of_subscription(referrer_sub) != PLAN_CREATOR:
                    row.update({"状態": STATUS_EXCLUDED, "メモ": "紹介者がチャンネル作成プランではありません"})
                    result["excluded"] += 1
                else:
                    amount = int(row["報酬額"] or 0)
                    _stripe().Customer.create_balance_transaction(
                        referrer["id"],
                        amount=-amount,  # マイナス＝クレジット（次回の会費から差し引かれる）
                        currency="jpy",
                        description=f"紹介特典（{row['入会した人']}さん・{row['支払日']}のお支払い分）",
                        metadata={"invoice": row["Stripe請求書ID"]},
                        idempotency_key=f"referral-reward-{row['Stripe請求書ID']}",  # 二重に付与しない
                    )
                    row.update({"状態": STATUS_GRANTED, "付与日": today, "メモ": ""})
                    result["granted"] += 1
                    result["amount"] += amount
            except Exception as error:  # noqa: BLE001 - 1件失敗しても続ける（次回また試す）
                row.update({"状態": STATUS_SCHEDULED, "メモ": f"付与エラー（次回再試行）：{error}"[:300]})
                result["errors"].append(f"{row['紹介者']}：{error}")
            _update(SHEET_REWARDS, row["_row"], row)
    return result


def sync_all_subscriptions() -> int:
    """Stripe のすべてのサブスクリプションを「契約者一覧」に反映する（管理者コマンド用）。件数を返す。"""
    count = 0
    with _lock:
        for subscription in _stripe().Subscription.list(status="all", limit=100).auto_paging_iter():
            _record_subscription(subscription)
            count += 1
            time.sleep(1.1)  # シートの書き込み回数の上限（1分あたり60回）に当たらないように
    return count


def member_summary(discord_id: str) -> dict:
    """紹介者本人向けの集計（紹介した人数・付与済み・予定の報酬）。"""
    with _lock:
        mine = get_code_for_member(discord_id)
        if mine is None:
            return {"referred": 0, "granted": 0, "scheduled": 0}
        code = mine["紹介コード"]
        referred = sum(1 for r in _rows(SHEET_REFERRALS) if r["紹介コード"] == code)
        rewards = [r for r in _rows(SHEET_REWARDS) if r["紹介コード"] == code]
    return {
        "referred": referred,
        "granted": sum(int(r["報酬額"] or 0) for r in rewards if r["状態"] == STATUS_GRANTED),
        "scheduled": sum(int(r["報酬額"] or 0) for r in rewards if r["状態"] == STATUS_SCHEDULED),
    }
