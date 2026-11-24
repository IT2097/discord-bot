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
   ただし、クレジットは「次回の会費1回分」までにする。会費を超える分は「振込集計」シートに
   月ごと・紹介者ごとにまとめ、運営が手で口座に振り込む（振り込んだら状態を「振込済み」にする）。

【報酬のルール（紹介制度規約より）】
・紹介された人のプランで、スタンダードプラン（2,200円）なら月500円、アフィリエイトプラン（3,300円）なら月750円、
  プレミアムプラン（4,400円）なら月1,000円
・報酬を受け取れるのは、紹介された人が支払った時点で、紹介者がアフィリエイトプランか
  プレミアムプランの会員のときだけ（スタンダードプランの会員の紹介は記録だけ残す）
・付与する時点で紹介者が退会していたら付与しない
・紹介者本人による入会は対象外

【必要な環境変数】
STRIPE_API_KEY         … Stripe の秘密キー（制限付きキー推奨。顧客・サブスクリプション・請求書・Checkout の読み取り、
                          顧客の残高（Customer balance）とサブスクリプションの書き込み）
STRIPE_WEBHOOK_SECRET  … Webhook の署名シークレット（whsec_...）
（任意）STRIPE_PRICE_GENERAL / STRIPE_PRICE_AFFILIATE / STRIPE_PRICE_CREATOR … プランの価格ID。
        未設定なら金額（2,200円／3,300円／4,400円）で見分ける
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
PRICE_AFFILIATE = os.getenv("STRIPE_PRICE_AFFILIATE", "")
PRICE_CREATOR = os.getenv("STRIPE_PRICE_CREATOR", "")
REFERRAL_LINK_BASE = os.getenv("REFERRAL_LINK_BASE", "https://resense-inc.com/reje/")
# 会員が自分でプラン変更・解約できる Stripe のカスタマーポータルのログインURL（任意。Discordと契約が結び付いていない人向け）
PORTAL_LOGIN_URL = os.getenv("STRIPE_PORTAL_LOGIN_URL", "")
# プランごとに付ける Discord のロール（スタンダードはロールなし）
AFFILIATE_ROLE_NAME = os.getenv("AFFILIATE_ROLE_NAME", "⭐️").strip()
PREMIUM_ROLE_NAME = os.getenv("PREMIUM_ROLE_NAME", "🌟").strip()

PLAN_GENERAL = "スタンダードプラン"
PLAN_AFFILIATE = "アフィリエイトプラン"
PLAN_CREATOR = "プレミアムプラン"
REWARD_BY_PLAN = {PLAN_GENERAL: 500, PLAN_AFFILIATE: 750, PLAN_CREATOR: 1000}
_PLAN_BY_AMOUNT = {2200: PLAN_GENERAL, 3300: PLAN_AFFILIATE, 4400: PLAN_CREATOR}
REFERRER_PLANS = (PLAN_CREATOR, PLAN_AFFILIATE)  # 紹介特典を受け取れるプラン（先にあるほど優先）
_NOT_REFERRER_PLAN = "紹介者が紹介特典の対象プラン（アフィリエイト・プレミアム）ではありません"


def _referrer_rank(plan: str) -> int:
    """紹介特典を受け取れるプランほど小さい数（並べ替え用）。"""
    return REFERRER_PLANS.index(plan) if plan in REFERRER_PLANS else len(REFERRER_PLANS)

STATUS_SCHEDULED = "予定"
STATUS_GRANTED = "付与済み"
STATUS_EXCLUDED = "対象外"
STATUS_ERROR = "エラー"

SHEET_CODES = "紹介コード"
SHEET_REFERRALS = "紹介記録"
SHEET_REWARDS = "紹介報酬"
SHEET_SUBSCRIBERS = "契約者一覧"
SHEET_TRANSFERS = "振込集計"

TRANSFER_UNPAID = "未振込"

_HEADERS = {
    SHEET_CODES: ["紹介コード", "DiscordID", "名前", "Stripeのメールアドレス", "発行日時", "更新日時", "会費免除"],
    SHEET_REFERRALS: [
        "入会日時", "紹介コード", "紹介者", "紹介者DiscordID", "入会した人", "入会した人のメール",
        "プラン", "StripeサブスクリプションID", "Stripe顧客ID",
    ],
    SHEET_REWARDS: [
        "支払日", "紹介コード", "紹介者", "入会した人", "プラン", "報酬額", "会費から差し引き", "振込",
        "付与予定日", "状態", "付与日", "メモ", "Stripe請求書ID", "StripeサブスクリプションID",
    ],
    SHEET_TRANSFERS: ["付与月", "紹介コード", "紹介者", "DiscordID", "振込額", "状態", "振込日", "メモ"],
    SHEET_SUBSCRIBERS: [
        "Stripe顧客ID", "名前", "メールアドレス", "プラン", "状態", "開始日", "解約日",
        "紹介コード", "StripeサブスクリプションID", "更新日時", "DiscordID",
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
        else:
            # 列を後から足したときは、見出しの行も足す（今ある行の中身はそのまま）
            current = sheet.row_values(1)
            headers = _HEADERS[name]
            if len(current) < len(headers):
                if sheet.col_count < len(headers):
                    sheet.add_cols(len(headers) - sheet.col_count)
                sheet.update(range_name="A1", values=[headers], value_input_option="RAW")
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

def is_exempt(code_row: dict | None) -> bool:
    """
    会費免除の紹介者か（「紹介コード」シートの「会費免除」列に「はい」などが入っている人）。
    会費を払っていないので Stripe の契約は確かめず、紹介特典は全額を振込にする。
    """
    value = str((code_row or {}).get("会費免除", "")).strip().lower()
    return value in ("はい", "○", "◯", "yes", "true", "1", "免除")


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
    if PRICE_AFFILIATE and price_id == PRICE_AFFILIATE:
        return PLAN_AFFILIATE
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
    # 紹介特典を受け取れるプランを優先する
    alive.sort(key=lambda s: _referrer_rank(_plan_of_subscription(s)))
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
            if _plan_of_subscription(sub) in REFERRER_PLANS:
                return customer, sub
            if best[0] is None or best[1] is None:
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
        "DiscordID": _get(subscription, "metadata", "discord_id", default=""),
    }
    existing = next((r for r in _rows(SHEET_SUBSCRIBERS) if r["StripeサブスクリプションID"] == subscription["id"]), None)
    if existing is not None:
        if not item["紹介コード"]:
            item["紹介コード"] = existing["紹介コード"]
        if not item["DiscordID"]:
            item["DiscordID"] = existing["DiscordID"]
        _update(SHEET_SUBSCRIBERS, existing["_row"], item)
    else:
        _append(SHEET_SUBSCRIBERS, item)
    # Discordと結び付いている契約なら、プランに合わせてロールを付け外しする
    if item["DiscordID"]:
        _notify_roles(item["DiscordID"], item["プラン"], _get(subscription, "status") in _ALIVE_STATUSES)


# ---------------------------------------------------------------------------
# Discord との結び付け（決済後の参加・ロール・プラン変更）
# ---------------------------------------------------------------------------

_ALIVE_STATUSES = ("active", "trialing", "past_due")
_role_hook = None  # bot.py が登録する：(DiscordID, プラン, 契約が続いているか) を受け取ってロールを付け外しする


def set_role_hook(func):
    global _role_hook
    _role_hook = func


def _notify_roles(discord_id: str, plan: str, alive: bool):
    if _role_hook is None:
        return
    try:
        _role_hook(str(discord_id), plan, alive)
    except Exception as error:  # noqa: BLE001 - ロールの更新に失敗しても記録は続ける
        print(f"ロールの更新を依頼できませんでした（{discord_id}）：{error}")


def role_for_plan(plan: str) -> str:
    """プランに付けるロールの名前（ロールなしなら空文字）。"""
    return {PLAN_AFFILIATE: AFFILIATE_ROLE_NAME, PLAN_CREATOR: PREMIUM_ROLE_NAME}.get(plan, "")


def checkout_join_info(session_id: str) -> dict:
    """
    決済が終わった Checkout の情報（決済後の「Discordに参加」ページ用）。
    支払いが済んでいなければ ReferralError。
    """
    if not session_id or not session_id.startswith("cs_"):
        raise ReferralError("お申し込みの情報が見つかりません。お手数ですが運営にお問い合わせください。")
    session = _stripe().checkout.Session.retrieve(session_id)
    subscription_id = _get(session, "subscription", default="")
    if _get(session, "status") != "complete" or not subscription_id:
        raise ReferralError("お支払いがまだ完了していません。完了してから、もう一度このページを開いてください。")
    subscription = _stripe().Subscription.retrieve(subscription_id)
    return {
        "subscription_id": subscription_id,
        "plan": _plan_of_subscription(subscription),
        "discord_id": _get(subscription, "metadata", "discord_id", default=""),
    }


def link_discord(subscription_id: str, discord_id: str):
    """契約に DiscordID を結び付ける（以後、プラン変更・解約に合わせてロールを自動で付け外しする）。"""
    with _lock:
        _stripe().Subscription.modify(subscription_id, metadata={"discord_id": str(discord_id)})
        _record_subscription({"id": subscription_id})


def portal_url_for(discord_id: str, return_url: str) -> str | None:
    """
    Discordと結び付いた契約の、プラン変更画面（Stripe のカスタマーポータル）のURL。
    結び付いた契約が無ければ None（呼び出し側でポータルのログインURLを案内する）。
    """
    with _lock:
        rows = [r for r in _rows(SHEET_SUBSCRIBERS) if r["DiscordID"] == str(discord_id)]
    for row in reversed(rows):  # 新しい契約を優先
        subscription = _stripe().Subscription.retrieve(row["StripeサブスクリプションID"])
        if _get(subscription, "status") not in _ALIVE_STATUSES:
            continue
        session = _stripe().billing_portal.Session.create(
            customer=subscription["customer"],
            return_url=return_url,
            flow_data={
                "type": "subscription_update",
                "subscription_update": {"subscription": subscription["id"]},
            },
        )
        return session["url"]
    return None


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
        "会費から差し引き": "",
        "振込": "",
        "付与予定日": _end_of_next_month(int(paid_at)),
        "状態": STATUS_SCHEDULED,
        "付与日": "",
        "メモ": "",
        "Stripe請求書ID": invoice["id"],
        "StripeサブスクリプションID": subscription_id,
    }

    if is_exempt(code_row):
        # 会費免除の紹介者：Stripeの契約は確かめない（付与日に全額を振込にする）
        if not item["報酬額"]:
            item.update({"状態": STATUS_EXCLUDED, "メモ": f"プランを判別できません（{plan}）"})
        else:
            item["メモ"] = "会費免除の紹介者（全額振込）"
        _append(SHEET_REWARDS, item)
        return

    # 支払った時点で、紹介者が紹介特典の対象プランか確かめる（規約：スタンダードプランの紹介者には報酬なし）
    referrer, referrer_sub = _referrer_customer(code_row)
    if not item["報酬額"]:
        item.update({"状態": STATUS_EXCLUDED, "メモ": f"プランを判別できません（{plan}）"})
    elif referrer is not None and referrer["id"] == customer["id"]:
        item.update({"状態": STATUS_EXCLUDED, "メモ": "紹介者本人による入会"})
    elif referrer is None:
        # メールアドレスの間違いなどもあり得るので、付与日に改めて確かめる
        item["メモ"] = "紹介者のStripeの契約が見つかりません（付与日に再確認）"
    elif referrer_sub is None or _plan_of_subscription(referrer_sub) not in REFERRER_PLANS:
        item.update({"状態": STATUS_EXCLUDED, "メモ": _NOT_REFERRER_PLAN})
    _append(SHEET_REWARDS, item)


# ---------------------------------------------------------------------------
# 報酬の付与（定期処理・管理者コマンドから呼ぶ）
# ---------------------------------------------------------------------------

def _monthly_fee(subscription) -> int:
    """紹介者の毎月の会費（クレジットの上限に使う）。"""
    for item in _get(subscription, "items", "data", default=[]) or []:
        amount = _get(item, "price", "unit_amount", default=0) or 0
        if amount:
            return int(amount) * int(_get(item, "quantity", default=1) or 1)
    return 4400


def _invoice_refunded(invoice_id: str) -> bool:
    """請求書の支払いが返金・取り消しされたか（紹介制度規約 第4条：返金された決済の特典は発生しない）。"""
    invoice = _stripe().Invoice.retrieve(invoice_id)
    if _get(invoice, "status") in ("void", "uncollectible"):
        return True
    charge_ids = []
    if _get(invoice, "charge"):  # 古いAPIのバージョン
        charge_ids.append(invoice["charge"])
    else:  # 新しいAPIのバージョンでは、請求書の支払い（InvoicePayment）からたどる
        try:
            for payment in _stripe().InvoicePayment.list(invoice=invoice_id, limit=10).data:
                charge = _get(payment, "payment", "charge")
                intent = _get(payment, "payment", "payment_intent")
                if charge:
                    charge_ids.append(charge)
                elif intent:
                    latest = _get(_stripe().PaymentIntent.retrieve(intent), "latest_charge")
                    if latest:
                        charge_ids.append(latest)
        except Exception as error:  # noqa: BLE001 - 確かめられないときは返金なしとして続ける
            print(f"請求書 {invoice_id} の支払いを確認できませんでした：{error}")
    for charge_id in charge_ids:
        charge = _stripe().Charge.retrieve(charge_id)
        if _get(charge, "refunded") or (_get(charge, "amount_refunded", default=0) or 0) > 0:
            return True
    return False


def grant_due_rewards() -> dict:
    """
    付与予定日が来た報酬を渡す。
    ・紹介者の Stripe の残高にクレジットとして入れる（次回の会費から差し引かれる）。ただし、
      まだ使われていないクレジットと合わせて「会費1回分」までにする
    ・会費を超える分は「振込」として記録し、「振込集計」シートに月ごと・紹介者ごとにまとめる
    """
    result = {"granted": 0, "amount": 0, "credit": 0, "transfer": 0, "excluded": 0, "errors": []}
    today = datetime.now(JST).strftime("%Y-%m-%d")
    with _lock:
        codes = {r["紹介コード"]: r for r in _rows(SHEET_CODES)}
        outstanding: dict[str, int] = {}  # 紹介者の顧客ID → まだ使われていないクレジット（円）
        for row in _rows(SHEET_REWARDS):
            if row["状態"] != STATUS_SCHEDULED or not row["付与予定日"] or row["付与予定日"] > today:
                continue
            code_row = codes.get(row["紹介コード"])
            try:
                if is_exempt(code_row):
                    # 会費免除の紹介者：会費から差し引けないので、全額を振込にする
                    if row["Stripe請求書ID"] and _invoice_refunded(row["Stripe請求書ID"]):
                        row.update({"状態": STATUS_EXCLUDED, "メモ": "紹介された方の決済が返金・取り消しされました"})
                        result["excluded"] += 1
                    else:
                        amount = int(row["報酬額"] or 0)
                        row.update({
                            "状態": STATUS_GRANTED, "付与日": today, "会費から差し引き": 0, "振込": amount,
                            "メモ": "会費免除の紹介者（全額振込）",
                        })
                        result["granted"] += 1
                        result["amount"] += amount
                        result["transfer"] += amount
                    _update(SHEET_REWARDS, row["_row"], row)
                    continue
                referrer, referrer_sub = _referrer_customer(code_row) if code_row else (None, None)
                if referrer is None or referrer_sub is None:
                    row.update({"状態": STATUS_EXCLUDED, "メモ": "付与日に紹介者の契約が見つかりません（退会・メールアドレス違い）"})
                    result["excluded"] += 1
                elif row["Stripe請求書ID"] and _invoice_refunded(row["Stripe請求書ID"]):
                    row.update({"状態": STATUS_EXCLUDED, "メモ": "紹介された方の決済が返金・取り消しされました"})
                    result["excluded"] += 1
                elif "紹介者のStripeの契約が見つかりません" in row["メモ"] and _plan_of_subscription(referrer_sub) not in REFERRER_PLANS:
                    row.update({"状態": STATUS_EXCLUDED, "メモ": _NOT_REFERRER_PLAN})
                    result["excluded"] += 1
                else:
                    amount = int(row["報酬額"] or 0)
                    if referrer["id"] not in outstanding:
                        balance = _get(_customer(referrer["id"]), "balance", default=0) or 0
                        outstanding[referrer["id"]] = max(0, -int(balance))  # マイナスの残高＝クレジット
                    room = max(0, _monthly_fee(referrer_sub) - outstanding[referrer["id"]])
                    credit = min(amount, room)
                    transfer = amount - credit
                    if credit:
                        _stripe().Customer.create_balance_transaction(
                            referrer["id"],
                            amount=-credit,  # マイナス＝クレジット（次回の会費から差し引かれる）
                            currency="jpy",
                            description=f"紹介特典（{row['入会した人']}さん・{row['支払日']}のお支払い分）",
                            metadata={"invoice": row["Stripe請求書ID"]},
                            idempotency_key=f"referral-reward-{row['Stripe請求書ID']}",  # 二重に付与しない
                        )
                        outstanding[referrer["id"]] += credit
                    row.update({
                        "状態": STATUS_GRANTED, "付与日": today, "会費から差し引き": credit, "振込": transfer,
                        "メモ": "会費を超える分は振込" if transfer else "",
                    })
                    result["granted"] += 1
                    result["amount"] += amount
                    result["credit"] += credit
                    result["transfer"] += transfer
            except Exception as error:  # noqa: BLE001 - 1件失敗しても続ける（次回また試す）
                row.update({"状態": STATUS_SCHEDULED, "メモ": f"付与エラー（次回再試行）：{error}"[:300]})
                result["errors"].append(f"{row['紹介者']}：{error}")
            _update(SHEET_REWARDS, row["_row"], row)
        _rebuild_transfers(codes)
    return result


def _rebuild_transfers(codes: dict[str, dict]):
    """
    「紹介報酬」の振込の分を、付与した月・紹介者ごとに合計して「振込集計」シートに書く。
    毎回「紹介報酬」から計算し直すので、途中で止まっても合計がずれない。
    運営が書き込んだ「状態」「振込日」「メモ」はそのまま残す。
    """
    totals: dict[tuple[str, str], int] = {}
    for row in _rows(SHEET_REWARDS):
        amount = int(row["振込"] or 0) if str(row["振込"]).strip().lstrip("-").isdigit() else 0
        if row["状態"] == STATUS_GRANTED and amount > 0 and row["付与日"]:
            key = (row["付与日"][:7], row["紹介コード"])
            totals[key] = totals.get(key, 0) + amount
    existing = {(r["付与月"], r["紹介コード"]): r for r in _rows(SHEET_TRANSFERS)}
    for (month, code), amount in sorted(totals.items()):
        code_row = codes.get(code, {})
        row = existing.get((month, code))
        if row is None:
            _append(SHEET_TRANSFERS, {
                "付与月": month, "紹介コード": code, "紹介者": code_row.get("名前", ""),
                "DiscordID": code_row.get("DiscordID", ""), "振込額": amount, "状態": TRANSFER_UNPAID,
            })
        elif str(row["振込額"]) != str(amount):
            if row["状態"] and row["状態"] != TRANSFER_UNPAID:
                row["メモ"] = (f"振込額が {row['振込額']}円 → {amount}円 に変わりました。差額をご確認ください。 " + row["メモ"])[:300]
            row["振込額"] = amount
            _update(SHEET_TRANSFERS, row["_row"], row)


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
            return {"referred": 0, "granted": 0, "credit": 0, "transfer": 0, "scheduled": 0}
        code = mine["紹介コード"]
        referred = sum(1 for r in _rows(SHEET_REFERRALS) if r["紹介コード"] == code)
        rewards = [r for r in _rows(SHEET_REWARDS) if r["紹介コード"] == code]
    return {
        "referred": referred,
        "granted": sum(int(r["報酬額"] or 0) for r in rewards if r["状態"] == STATUS_GRANTED),
        "credit": sum(int(r["会費から差し引き"] or 0) for r in rewards if r["状態"] == STATUS_GRANTED),
        "transfer": sum(int(r["振込"] or 0) for r in rewards if r["状態"] == STATUS_GRANTED),
        "scheduled": sum(int(r["報酬額"] or 0) for r in rewards if r["状態"] == STATUS_SCHEDULED),
    }
