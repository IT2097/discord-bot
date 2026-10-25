"""
マッチング申請の承認・拒否を「承認」チャンネルで行うためのViewと、
プライベートなマッチングルーム作成処理をまとめたモジュール。
"""

import os
import asyncio
from datetime import datetime, timedelta, timezone

import discord

import match_store
import match_records
import sheets_client

GUILD_ID = os.getenv("GUILD_ID")

# マッチング申請の承認メッセージを投稿するテキストチャンネル名
APPROVAL_CHANNEL_NAME = os.getenv("APPROVAL_CHANNEL_NAME", "承認")

# マッチングルームをまとめて入れるカテゴリ名。
# オーナーはこのカテゴリをミュート＋「ミュートしたチャンネルを非表示」にすることで、
# 他人のマッチングルームをサイドバーに出さないようにできる。
# Discordは1カテゴリに最大50チャンネルまでのため、埋まったら「〇〇 2」「〇〇 3」…を自動で作る。
MATCH_CATEGORY_NAME = os.getenv("MATCH_CATEGORY_NAME", "マッチングルーム一覧")
_CATEGORY_CHANNEL_LIMIT = 50


def _is_match_category(category: discord.CategoryChannel) -> bool:
    name = category.name
    if name == MATCH_CATEGORY_NAME:
        return True
    prefix = f"{MATCH_CATEGORY_NAME} "
    return name.startswith(prefix) and name[len(prefix):].isdigit()


async def get_match_category(guild: discord.Guild) -> discord.CategoryChannel:
    """
    マッチングルームを入れるカテゴリを返す（空きのある既存カテゴリ、無ければ新規作成）。
    カテゴリ自体も@everyoneからは見えないようにしておく。
    """
    categories = sorted(
        (c for c in guild.categories if _is_match_category(c)), key=lambda c: c.position
    )
    for category in categories:
        if len(category.channels) < _CATEGORY_CHANNEL_LIMIT:
            return category

    name = MATCH_CATEGORY_NAME if not categories else f"{MATCH_CATEGORY_NAME} {len(categories) + 1}"
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, manage_channels=True),
    }
    return await guild.create_category(name=name, overwrites=overwrites)


def is_private_match_room(channel: discord.TextChannel) -> list[str] | None:
    """
    「@everyoneは見えない」かつ「Bot以外の個人2人だけが明示的に見える」チャンネルなら、
    その2人のIDを返す（マッチングルームとみなす）。違えば None。
    """
    guild = channel.guild
    member_ids = []
    default_denied = False
    for target, overwrite in channel.overwrites.items():
        if target == guild.default_role:
            if overwrite.view_channel is False:
                default_denied = True
            continue
        if isinstance(target, discord.Member) and not target.bot and overwrite.view_channel is True:
            member_ids.append(str(target.id))
    if default_denied and len(member_ids) == 2:
        return member_ids
    return None


def _format_profile(member: dict) -> str:
    """
    スプレッドシートのプロフィール情報（名前・年齢・性別・都道府県・市町村・
    業種・事業/活動内容）を、DMやチャンネル投稿用のテキストに整形する。
    マッチング成立時に自動投稿するプロフィール用（A〜H列相当のみ）。
    """
    lines = [f"📇 {member['name']} さんのプロフィールです"]

    if member.get("age"):
        lines.append(f"年齢：{member['age']}")
    if member.get("gender"):
        lines.append(f"性別：{member['gender']}")
    if member.get("prefecture"):
        lines.append(f"都道府県：{member['prefecture']}")
    if member.get("city"):
        lines.append(f"市町村：{member['city']}")

    for biz in member.get("businesses", []):
        biz_type = biz.get("type", "")
        biz_content = biz.get("content", "")
        if biz_type:
            lines.append(f"業種：{biz_type}")
        if biz_content:
            lines.append(f"事業/活動内容：{biz_content}")

    return "\n".join(lines)


def _format_full_profile(member: dict) -> str:
    """
    マッチング会員一覧（Webページ）に表示されているのと同じ情報一式
    （年齢・性別・都道府県・市町村・業種・事業内容・URL）を整形する。
    承認チャンネルの「プロフィール」ボタンの表示用。
    """
    lines = [f"📇 {member['name']} さんのプロフィールです"]

    if member.get("age"):
        lines.append(f"年齢：{member['age']}")
    if member.get("gender"):
        lines.append(f"性別：{member['gender']}")
    if member.get("prefecture"):
        lines.append(f"都道府県：{member['prefecture']}")
    if member.get("city"):
        lines.append(f"市町村：{member['city']}")

    for biz in member.get("businesses", []):
        biz_type = biz.get("type", "")
        biz_content = biz.get("content", "")
        if biz_type:
            lines.append(f"業種：{biz_type}")
        if biz_content:
            lines.append(f"事業/活動内容：{biz_content}")

    if member.get("urls"):
        lines.append("URL：")
        lines.extend(member["urls"])

    return "\n".join(lines)


async def create_match_channel(
    guild: discord.Guild, member_a: discord.Member, member_b: discord.Member
) -> discord.TextChannel:
    """2人だけが見えるプライベートなマッチングルームを作成する。"""
    bot_member = guild.me

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        member_a: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        ),
        member_b: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        ),
        bot_member: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            manage_channels=True,
            read_message_history=True,
        ),
    }

    # 専用カテゴリにまとめて作る（取得に失敗してもルーム作成自体は続行する）
    try:
        category = await get_match_category(guild)
    except discord.HTTPException:
        category = None

    channel = await guild.create_text_channel(
        name=f"{member_a.display_name}-{member_b.display_name}",
        overwrites=overwrites,
        category=category,
    )

    await channel.send(f"{member_a.mention} と {member_b.mention} のマッチングルームです！")

    # お互いのプロフィール（スプレッドシートの情報）を1通ずつ投稿する
    for member in (member_a, member_b):
        try:
            profile = sheets_client.find_member(str(member.id))
        except Exception:  # noqa: BLE001 - シート取得に失敗してもルーム作成自体は継続する
            profile = None
        if profile:
            await channel.send(_format_profile(profile))

    return channel



# ---------------------------------------------------------------------------
# 承認チャンネルのボタン（承認する / 断る / プロフィール）
#
# ボタンの custom_id に申請ID を埋め込み（例：match_approve:1a2b...）、
# DynamicItem として登録しておくことで、botが再起動・デプロイしても
# 以前に投稿したボタンが押せるようにしている。
# 申請の中身（誰から誰へ・いつ・状態）は match_store がスプレッドシートに保存している。
# ---------------------------------------------------------------------------

_JST = timezone(timedelta(hours=9))
_WEEKDAYS = "月火水木金土日"


def _format_jst(timestamp: float) -> str:
    """日時を日本時間・日本語の表記にする（例：2026年10月28日(水) 17:04）。"""
    dt = datetime.fromtimestamp(timestamp, _JST)
    return f"{dt.year}年{dt.month}月{dt.day}日({_WEEKDAYS[dt.weekday()]}) {dt:%H:%M}"


_EXPIRED_TEXT = "この申請は有効期限（7日間）が切れています。申請者は、もう一度申請し直すことができます。"
_DONE_TEXT = "この申請はすでに対応済みです。"


async def _load_request_for_target(interaction: discord.Interaction, request_id: str) -> dict | None:
    """申請を読み込み、押した人が宛先本人か・まだ承認待ちかを確認する。問題があれば本人に伝えて None を返す。"""
    request = await asyncio.to_thread(match_store.get_request, request_id)

    if request is None:
        await interaction.response.send_message("この申請は見つかりませんでした。", ephemeral=True)
        return None

    if str(interaction.user.id) != request["target_id"]:
        await interaction.response.send_message(
            "この申請はあなた宛てではないため、操作できません。", ephemeral=True
        )
        return None

    if request["status"] == match_store.STATUS_EXPIRED:
        await interaction.response.send_message(_EXPIRED_TEXT, ephemeral=True)
        await _close_request_message(interaction.message, "⌛ 有効期限が切れました")
        if _is_request_thread(interaction.message):
            _schedule_thread_removal(interaction.message, None, THREAD_DELETE_DELAY_SECONDS)
        return None

    if request["status"] == match_store.STATUS_CANCELLED:
        await interaction.response.send_message("この申請は、申請者が取り消しました。", ephemeral=True)
        await _delete_request_message(interaction.message, "↩️ 申請者が取り消しました")
        return None

    if request["status"] != match_store.STATUS_PENDING:
        await interaction.response.send_message(_DONE_TEXT, ephemeral=True)
        return None

    return request


async def _close_request_message(message: discord.Message | None, note: str):
    """承認チャンネルのメッセージのボタンを押せなくし、結果を1行追記する。"""
    if message is None:
        return
    content = message.content or ""
    if note not in content:
        content = f"{content}\n\n{note}"
    try:
        await message.edit(content=content, view=_build_request_view("closed", disabled=True))
    except discord.HTTPException:
        pass


# ---------------------------------------------------------------------------
# 申請ごとのプライベートスレッド
#
# 承認チャンネルに直接投稿すると会員全員に見えてしまうため、申請ごとに
# 「申請された人だけが入れるプライベートスレッド」を作り、その中にボタンを出す。
# 申請が終わったら（承認・断る・取り消し・期限切れ）スレッドは削除する。
# 以前に承認チャンネルへ直接投稿した申請は、これまでどおりメッセージだけを扱う。
# ---------------------------------------------------------------------------

THREAD_DELETE_DELAY_SECONDS = 60  # 承認・断るを押したあと、結果を読めるよう少し待ってから消す
_background_tasks: set[asyncio.Task] = set()


def _is_request_thread(message: discord.Message | None) -> bool:
    return message is not None and isinstance(message.channel, discord.Thread)


async def _remove_request_thread(message: discord.Message, note: str | None, delay: float):
    """スレッドのボタンを閉じて結果を書き足し、delay 秒後にスレッドごと削除する。"""
    thread = message.channel
    if note:
        await _close_request_message(message, note)
    if delay:
        await asyncio.sleep(delay)
    try:
        await thread.delete()
    except discord.HTTPException:
        # 削除できない（権限不足など）ときは、せめてアーカイブ・ロックして一覧から消す
        try:
            await thread.edit(archived=True, locked=True)
        except discord.HTTPException:
            pass


def _schedule_thread_removal(message: discord.Message, note: str | None, delay: float):
    task = asyncio.create_task(_remove_request_thread(message, note, delay))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _delete_request_message(
    message: discord.Message | None,
    fallback_note: str = "✅ 承認されました",
    thread_delay: float = THREAD_DELETE_DELAY_SECONDS,
):
    """
    承認チャンネルの申請メッセージを削除する。
    申請がプライベートスレッドの中にある場合は、結果を書き足してから thread_delay 秒後にスレッドごと削除する。
    その申請に返信する形で送ったリマインドのメッセージも、あわせて削除する。
    削除できなかった場合は、ボタンを押せなくして「承認されました」と追記する。
    """
    if message is None:
        return

    if _is_request_thread(message):
        _schedule_thread_removal(message, fallback_note if thread_delay else None, thread_delay)
        return

    try:
        async for later in message.channel.history(limit=200, after=message):
            if later.author.id == message.author.id and later.reference and later.reference.message_id == message.id:
                try:
                    await later.delete()
                except discord.HTTPException:
                    pass
    except discord.HTTPException:
        pass

    try:
        await message.delete()
    except discord.HTTPException:
        await _close_request_message(message, fallback_note)


async def _handle_approve(interaction: discord.Interaction, request_id: str):
    request = await _load_request_for_target(interaction, request_id)
    if request is None:
        return

    await interaction.response.defer()
    resolved = await asyncio.to_thread(match_store.resolve_request, request_id, match_store.STATUS_APPROVED)
    if resolved is None:
        await interaction.followup.send(_DONE_TEXT, ephemeral=True)  # 同時に2回押された場合
        return
    # 承認されたら、承認チャンネルの申請メッセージ（とリマインドの返信）は不要なので削除する
    await _delete_request_message(interaction.message)

    guild = interaction.guild or (interaction.client.get_guild(int(GUILD_ID)) if GUILD_ID else None)
    if guild is None:
        await interaction.followup.send("サーバー情報が取得できませんでした。管理者に連絡してください。", ephemeral=True)
        return

    try:
        requester = await guild.fetch_member(int(request["requester_id"]))
        target = await guild.fetch_member(int(request["target_id"]))
    except discord.NotFound:
        await interaction.followup.send(
            "相手がサーバーに見つかりませんでした。すでに退会された可能性があります。", ephemeral=True
        )
        return

    try:
        channel = await create_match_channel(guild, requester, target)
    except discord.Forbidden:
        await interaction.followup.send(
            "Botに「チャンネルを管理」の権限がありません。管理者に連絡してください。", ephemeral=True
        )
        return
    except discord.HTTPException as error:
        await interaction.followup.send(f"チャンネル作成に失敗しました：{error}", ephemeral=True)
        return

    await interaction.followup.send(
        f"マッチングが成立しました！ {channel.mention} をご確認ください。", ephemeral=True
    )

    match_store.mark_matched(request["requester_id"], request["target_id"])
    try:
        # スプレッドシートへの書き込みはブロッキング処理なので別スレッドで実行する
        await asyncio.to_thread(match_records.append_match, request["requester_id"], request["target_id"])
    except Exception:  # noqa: BLE001 - 永続化に失敗してもマッチング自体は成立させる
        print("マッチング履歴の永続化に失敗しました。")

    try:
        await requester.send(
            f"{target.display_name} さんがマッチング申請を承認しました！ {channel.mention} をご確認ください。"
        )
    except discord.Forbidden:
        pass  # DMを閉じているユーザーには通知できない


async def _handle_reject(interaction: discord.Interaction, request_id: str):
    request = await _load_request_for_target(interaction, request_id)
    if request is None:
        return

    await interaction.response.defer()
    resolved = await asyncio.to_thread(match_store.resolve_request, request_id, match_store.STATUS_REJECTED)
    if resolved is None:
        await interaction.followup.send(_DONE_TEXT, ephemeral=True)
        return
    await _close_request_message(interaction.message, "見送りになりました")
    await interaction.followup.send("申請を断りました。", ephemeral=True)
    if _is_request_thread(interaction.message):
        _schedule_thread_removal(interaction.message, None, THREAD_DELETE_DELAY_SECONDS)

    guild = interaction.guild
    if guild is None:
        return
    try:
        requester = await guild.fetch_member(int(request["requester_id"]))
        await requester.send("残念ながら、マッチング申請は今回見送られました。")
    except (discord.NotFound, discord.Forbidden):
        pass


async def _handle_profile(interaction: discord.Interaction, request_id: str):
    """
    申請してきた相手のプロフィールを、押した本人にだけ見える形（ephemeral）で表示する。
    内容はマッチング会員一覧（Webページ）に表示されているものと同じ情報一式。
    """
    request = await _load_request_for_target(interaction, request_id)
    if request is None:
        return

    await interaction.response.defer(ephemeral=True)
    try:
        profile = await asyncio.to_thread(sheets_client.find_member, request["requester_id"])
    except Exception as error:  # noqa: BLE001 - そのままエラー内容を見せる
        await interaction.followup.send(f"プロフィールの取得に失敗しました：{error}", ephemeral=True)
        return

    if profile is None:
        await interaction.followup.send("プロフィール情報が見つかりませんでした。", ephemeral=True)
        return
    await interaction.followup.send(_format_full_profile(profile), ephemeral=True)


class ApproveButton(discord.ui.DynamicItem[discord.ui.Button], template=r"match_approve:(?P<rid>[0-9a-f]{32})"):
    def __init__(self, request_id: str):
        super().__init__(
            discord.ui.Button(label="承認する", style=discord.ButtonStyle.success, custom_id=f"match_approve:{request_id}")
        )
        self.request_id = request_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["rid"])

    async def callback(self, interaction: discord.Interaction):
        await _handle_approve(interaction, self.request_id)


class RejectButton(discord.ui.DynamicItem[discord.ui.Button], template=r"match_reject:(?P<rid>[0-9a-f]{32})"):
    def __init__(self, request_id: str):
        super().__init__(
            discord.ui.Button(label="断る", style=discord.ButtonStyle.danger, custom_id=f"match_reject:{request_id}")
        )
        self.request_id = request_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["rid"])

    async def callback(self, interaction: discord.Interaction):
        await _handle_reject(interaction, self.request_id)


class ProfileButton(discord.ui.DynamicItem[discord.ui.Button], template=r"match_profile:(?P<rid>[0-9a-f]{32})"):
    def __init__(self, request_id: str):
        super().__init__(
            discord.ui.Button(label="プロフィール", style=discord.ButtonStyle.secondary, custom_id=f"match_profile:{request_id}")
        )
        self.request_id = request_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["rid"])

    async def callback(self, interaction: discord.Interaction):
        await _handle_profile(interaction, self.request_id)


# botの起動時に bot.add_dynamic_items(*REQUEST_BUTTONS) で登録する
REQUEST_BUTTONS = (ApproveButton, RejectButton, ProfileButton)


def _build_request_view(request_id: str, disabled: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    if disabled:
        # 対応済み・期限切れのメッセージ用（押せない飾りのボタン）
        for label, style in (("承認する", discord.ButtonStyle.success), ("断る", discord.ButtonStyle.danger), ("プロフィール", discord.ButtonStyle.secondary)):
            view.add_item(discord.ui.Button(label=label, style=style, disabled=True))
        return view
    view.add_item(ApproveButton(request_id))
    view.add_item(RejectButton(request_id))
    view.add_item(ProfileButton(request_id))
    return view


# 承認チャンネルでbotに必要な権限（申請ごとのプライベートスレッドを作って、終わったら消すため）
APPROVAL_BOT_PERMISSIONS = dict(
    view_channel=True,
    send_messages=True,
    read_message_history=True,
    embed_links=True,
    manage_messages=True,
    create_private_threads=True,
    send_messages_in_threads=True,
    manage_threads=True,
)


async def setup_approval_channel(guild: discord.Guild, member_role: discord.Role | None) -> discord.TextChannel:
    """
    承認チャンネルの権限を「申請ごとのプライベートスレッド」方式に合わせて設定する。
    - bot：スレッドの作成・削除ができる
    - @everyone：見えない
    - 会員ロール：チャンネルは見えるが、投稿やスレッドの作成はできない
      （自分が追加されたスレッド＝自分宛ての申請だけが見える）
    """
    channel = discord.utils.get(guild.text_channels, name=APPROVAL_CHANNEL_NAME)
    if channel is None:
        raise RuntimeError(f"「{APPROVAL_CHANNEL_NAME}」という名前のテキストチャンネルが見つかりません。")
    await channel.set_permissions(guild.me, **APPROVAL_BOT_PERMISSIONS)
    await channel.set_permissions(guild.default_role, view_channel=False)
    if member_role is not None:
        await channel.set_permissions(
            member_role,
            view_channel=True,
            read_message_history=True,
            send_messages=False,
            add_reactions=False,
            create_public_threads=False,
            create_private_threads=False,
            send_messages_in_threads=False,
        )
    return channel


async def send_match_request_to_channel(bot: discord.Client, requester_id: str, target_id: str, request_id: str):
    """
    Webページからのマッチング申請を、「承認」チャンネルに投稿して通知する。
    申請の宛先ユーザーをメンションし、承認/断るボタンを添える。
    """
    if not GUILD_ID:
        raise RuntimeError("環境変数 GUILD_ID が設定されていません。")

    guild = bot.get_guild(int(GUILD_ID))
    if guild is None:
        raise RuntimeError("Botが指定のサーバーに参加していないか、GUILD_IDが誤っています。")

    channel = discord.utils.get(guild.text_channels, name=APPROVAL_CHANNEL_NAME)
    if channel is None:
        raise RuntimeError(f"「{APPROVAL_CHANNEL_NAME}」という名前のテキストチャンネルが見つかりません。")

    requester = await guild.fetch_member(int(requester_id))
    target = await guild.fetch_member(int(target_id))

    request = await asyncio.to_thread(match_store.get_request, request_id)
    expires_text = ""
    if request:
        expires_text = f"\n（有効期限：{_format_jst(request['expires_at'])} まで）"

    try:
        # 申請された人だけが見られるプライベートスレッドを作り、その中にボタンを出す
        thread = await channel.create_thread(
            name=f"{requester.display_name}さんからのマッチング申請"[:100],
            type=discord.ChannelType.private_thread,
            invitable=False,
            auto_archive_duration=10080,  # 7日間（申請の有効期限と同じ）
        )
        await thread.add_user(target)
        message = await thread.send(
            f"{target.mention}\n{requester.display_name} さんからマッチの依頼が来ました！承認しますか？\n"
            f"（「プロフィール」ボタンから相手の情報を確認できます）{expires_text}\n"
            "※このスレッドは、あなたにだけ表示されています。",
            view=_build_request_view(request_id),
        )
    except discord.Forbidden as error:
        raise RuntimeError(
            f"Botが「{APPROVAL_CHANNEL_NAME}」チャンネルでプライベートスレッドを作る権限を持っていません。"
            "管理者の方は !承認チャンネル設定 を実行してください。"
        ) from error

    # リマインドと期限切れの表示のために、投稿したメッセージの場所（スレッドのID）を記録する
    try:
        await asyncio.to_thread(match_store.set_request_message, request_id, thread.id, message.id)
    except Exception as error:  # noqa: BLE001 - 記録に失敗しても申請自体は届いている
        print(f"申請メッセージの記録に失敗しました：{error}")


async def delete_cancelled_request_message(bot: discord.Client, request: dict):
    """
    申請者が取り消した申請の、承認チャンネルのメッセージ（とリマインド）を削除する。
    削除できなかった場合は、ボタンを押せなくして「取り消されました」と追記する。
    """
    message = await _fetch_request_message(bot, request)
    # 取り消した申請は、相手に見せる必要がないのですぐに消す
    await _delete_request_message(message, "↩️ 申請者が取り消しました", thread_delay=0)


async def migrate_approval_channel(bot: discord.Client, channel: discord.TextChannel) -> dict:
    """
    以前の方式（承認チャンネルに直接投稿）の申請を、プライベートスレッド方式に移す。
    - 承認待ちの申請は、スレッドを作って投稿し直し、元のメッセージを消す（相手にはメンション通知がもう一度届く）
    - 承認チャンネルに残っている、終わった申請やリマインドなどのbotのメッセージは削除する
    """
    moved, failed = 0, 0
    keep_ids: set[int] = set()
    for request in await asyncio.to_thread(match_store.pending_requests):
        if request.get("channel_id") != str(channel.id):
            continue  # すでにスレッド方式の申請
        old_message = await _fetch_request_message(bot, request)
        try:
            await send_match_request_to_channel(
                bot, request["requester_id"], request["target_id"], request["request_id"]
            )
            moved += 1
        except Exception as error:  # noqa: BLE001 - 1件失敗しても他は続ける
            print(f"申請 {request['request_id']} の移行に失敗しました：{error}")
            failed += 1
            if old_message is not None:
                keep_ids.add(old_message.id)  # 失敗した申請の元のメッセージは消さずに残す
            continue
        if old_message is not None:
            try:
                await old_message.delete()
            except discord.HTTPException:
                pass

    deleted = await channel.purge(
        limit=2000,
        check=lambda m: m.author.id == bot.user.id and m.id not in keep_ids,
    )
    return {"moved": moved, "failed": failed, "deleted": len(deleted)}


# ---------------------------------------------------------------------------
# リマインド・期限切れの定期処理（botから30分ごとに呼び出す）
# ---------------------------------------------------------------------------

async def _fetch_request_message(bot: discord.Client, request: dict) -> discord.Message | None:
    if not request.get("channel_id") or not request.get("message_id"):
        return None
    try:
        channel = bot.get_channel(int(request["channel_id"])) or await bot.fetch_channel(int(request["channel_id"]))
        return await channel.fetch_message(int(request["message_id"]))
    except (discord.HTTPException, ValueError):
        return None


async def process_pending_requests(bot: discord.Client):
    """
    3日たっても未対応の申請は、相手に1回だけリマインドする。
    7日たった申請は「期限切れ」にしてボタンを押せなくし、申請者に再申請できることを伝える。
    """
    for request in await asyncio.to_thread(match_store.requests_to_expire):
        message = await _fetch_request_message(bot, request)
        if _is_request_thread(message):
            await _remove_request_thread(message, None, 0)  # 期限切れの申請スレッドは削除する
        else:
            await _close_request_message(message, "⌛ 有効期限が切れました")
        await asyncio.to_thread(match_store.mark_expired, request["request_id"])

        guild = bot.get_guild(int(GUILD_ID)) if GUILD_ID else None
        if guild is None:
            continue
        try:
            requester = await guild.fetch_member(int(request["requester_id"]))
            target = guild.get_member(int(request["target_id"]))
            target_name = target.display_name if target else "相手"
            await requester.send(
                f"{target_name} さんへのマッチング申請は、7日間返事がなかったため期限切れになりました。"
                "会員一覧ページから、もう一度申請できます。"
            )
        except (discord.NotFound, discord.Forbidden):
            pass

    for request in await asyncio.to_thread(match_store.requests_to_remind):
        message = await _fetch_request_message(bot, request)
        if message is not None:
            try:
                await message.reply(
                    f"<@{request['target_id']}> 未対応のマッチング申請があります。"
                    f"{_format_jst(request['expires_at'])} に期限が切れるので、承認するか断るかを選んでください。",
                    mention_author=False,
                )
            except discord.HTTPException:
                pass
        await asyncio.to_thread(match_store.mark_reminded, request["request_id"])


class LegacyRequestButton(discord.ui.DynamicItem[discord.ui.Button], template=r"(?P<kind>match_approve|match_reject|match_view_profile)"):
    """
    今回の改修より前に投稿された申請のボタン用。
    古い申請は再起動で中身が消えているため、押されたらボタンを閉じて再申請をお願いする。
    """

    def __init__(self, kind: str):
        super().__init__(discord.ui.Button(label="（旧）", custom_id=kind))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["kind"])

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "この申請は古い形式のため、受け付けを終了しました。申請者の方に、会員一覧ページからもう一度申請してもらってください。",
            ephemeral=True,
        )
        await _close_request_message(interaction.message, "⌛ 受付終了（再申請をお願いします）")


REQUEST_BUTTONS = (ApproveButton, RejectButton, ProfileButton, LegacyRequestButton)
