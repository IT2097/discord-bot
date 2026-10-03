from __future__ import annotations

import os
import asyncio
import io
import threading

import discord
from discord.ext import commands

import match_views
import match_store
import match_records
import sheet_sync
import sheets_client
import web_app

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Botのイベントループへの参照。on_ready で確定させる。
bot_loop: asyncio.AbstractEventLoop | None = None

# デプロイされているコードが最新かを確認するための目印（!id同期 の最初のメッセージに表示）
BOT_VERSION = "2026-10-03 登録誘導版"

# 会員向けの固定URL（会員情報の確認・マッチング申請ページ）を投稿するチャンネル名
MATCHING_ROOM_CHANNEL_NAME = os.getenv("MATCHING_ROOM_CHANNEL_NAME", "マッチングルーム")
WEB_APP_URL = os.getenv("WEB_APP_URL")

# 新規メンバー参加時に「id同期」の結果を投稿するチャンネル名
ID_SYNC_LOG_CHANNEL_NAME = os.getenv("ID_SYNC_LOG_CHANNEL_NAME", "id同期ログ")

# DMではなく、名前登録チャンネル内でメンション付きで通知するために使うチャンネル名
NAME_REGISTER_CHANNEL_NAME = os.getenv("NAME_REGISTER_CHANNEL_NAME", "名前登録")

# 参加直後に自動付与し、本名登録が終わったら自動で外すロール名
# （名前登録チャンネルも含め、すべてのチャンネルをこのロールから見えなくする想定。
#   名前登録チャンネルだけは、本人にのみ個別の閲覧権限を一時的に付与する）
UNVERIFIED_ROLE_NAME = os.getenv("UNVERIFIED_ROLE_NAME", "未登録")

# id同期の結果に「候補が複数」「未一致」がある場合にメンションするロール名
ADMIN_ROLE_NAME = os.getenv("ADMIN_ROLE_NAME", "管理者")

# 本名登録が完了したら自動付与するロール名。
# マッチングルーム・承認チャンネルなど「登録済みの会員だけに見せたい」チャンネルは、
# このロールを持つ人だけが見られる「許可リスト方式」（@everyoneは非表示、このロールだけ許可）に統一する。
# 「未登録ロールを禁止する」方式（deny方式）は設定漏れがあると素通しで見えてしまうため、
# こちらのより確実な方式に切り替える。
MEMBER_ROLE_NAME = os.getenv("MEMBER_ROLE_NAME", "会員")

# Botが自分で投稿・管理するチャンネル（承認・マッチングルーム・名前登録）に付けるBot用の権限
BOT_CHANNEL_PERMISSIONS = dict(
    view_channel=True,
    send_messages=True,
    read_message_history=True,
    embed_links=True,
    manage_messages=True,
)

# 「会員」ロールを持つ人だけに表示するチャンネル名（カンマ区切りで環境変数からも追加できる）
MEMBER_ONLY_CHANNEL_NAMES = [
    name.strip()
    for name in os.getenv(
        "MEMBER_ONLY_CHANNEL_NAMES",
        f"{MATCHING_ROOM_CHANNEL_NAME},{match_views.APPROVAL_CHANNEL_NAME}",
    ).split(",")
    if name.strip()
]


async def _remove_unverified_role(member: discord.Member):
    """本名登録が完了したメンバーから「未登録」ロールを外す。"""
    role = discord.utils.get(member.guild.roles, name=UNVERIFIED_ROLE_NAME)
    if role is None:
        return
    if role in member.roles:
        try:
            await member.remove_roles(role, reason="本名登録が完了したため")
        except discord.Forbidden:
            print(f"「{UNVERIFIED_ROLE_NAME}」ロールを外す権限がBotにありません。")


async def _grant_member_role(member: discord.Member):
    """本名登録が完了したメンバーに「会員」ロールを付与する（許可リスト方式のチャンネル閲覧用）。"""
    role = discord.utils.get(member.guild.roles, name=MEMBER_ROLE_NAME)
    if role is None:
        print(f"「{MEMBER_ROLE_NAME}」という名前のロールが見つかりませんでした。先にロールを作成してください。")
        return
    if role not in member.roles:
        try:
            await member.add_roles(role, reason="本名登録が完了したため")
        except discord.Forbidden:
            print(f"「{MEMBER_ROLE_NAME}」ロールを付与する権限がBotにありません。")


async def _grant_member_role_to_linked(guild: discord.Guild, linked_ids: list[str] | None = None) -> dict:
    """
    スプレッドシートとDiscordの紐づけが完了している人（DiscordIDが入っている人）全員に
    「会員」ロールを付与する。id同期のたびに呼び出すことで、
    今回新たに一致した人だけでなく、以前から紐づけ済みだった人
    （本番移行時にすでに参加している何十人もの既存会員など）にも
    もれなくロールが行き渡るようにする。
    あわせて「未登録」ロールが残っていれば外す
    （ニックネーム変更による自動同期で紐づいた人が、一般チャンネルを見られないままになるのを防ぐ）。

    戻り値：
      role_missing  … 「会員」ロールがサーバーに存在しない（先に作成が必要）
      granted       … 新たにロールを付与した人数
      failed        … 権限不足等で付与に失敗した人数
        （Botに「ロールの管理」権限が無い、またはBotの最上位ロールが
          「会員」ロールより下にある場合に発生する。Discordの仕様上、
          Botは自分より上位のロールを付与できないため）
    """
    result = {
        "role_missing": False,
        "targets": 0,         # 付与対象（サーバー内にいて紐づけ済み）の人数
        "granted": 0,         # 今回新たに付与した人数
        "already": 0,         # すでにロールを持っていた人数
        "failed": 0,          # 付与に失敗した人数
        "error": None,        # 失敗時のエラー内容（最初の1件）
    }

    role = discord.utils.get(guild.roles, name=MEMBER_ROLE_NAME)
    if role is None:
        print(f"「{MEMBER_ROLE_NAME}」という名前のロールが見つかりませんでした。先にロールを作成してください。")
        result["role_missing"] = True
        return result

    if linked_ids is None:
        # 対象IDが渡されなかった場合はシートから取得する
        try:
            members_data = await asyncio.to_thread(sheets_client.get_members, True)
        except Exception as error:  # noqa: BLE001 - 呼び出し元でエラー内容を表示する
            result["error"] = f"スプレッドシート取得エラー：{error}"
            return result
        linked_ids = [m["discord_id"] for m in members_data if m.get("discord_id")]

    for discord_id in set(linked_ids):
        try:
            discord_id_int = int(str(discord_id).strip())
        except ValueError:
            continue

        member = guild.get_member(discord_id_int)
        if member is None:
            try:
                member = await guild.fetch_member(discord_id_int)
            except discord.HTTPException:
                continue  # このサーバーにいない人

        if member.bot:
            continue

        result["targets"] += 1

        # 紐づけが済んでいる人に「未登録」ロールが残っていれば外す
        await _remove_unverified_role(member)

        if role in member.roles:
            result["already"] += 1
            continue

        try:
            await member.add_roles(role, reason="スプレッドシートとの紐づけが完了しているため")
            result["granted"] += 1
        except discord.HTTPException as error:
            result["failed"] += 1
            if result["error"] is None:
                result["error"] = f"{member.display_name}：{error}"
            print(f"「{MEMBER_ROLE_NAME}」ロールを {member}（{member.id}）に付与できませんでした：{error}")

    return result


def _format_role_result(role_result: dict) -> list[str]:
    """会員ロール付与の結果を、id同期の結果メッセージ用の行に整形する（常に1行以上出す）。"""
    if role_result["role_missing"]:
        return [
            f"⚠️「{MEMBER_ROLE_NAME}」という名前のロールが見つからないため、"
            "ロール付与をスキップしました。先にDiscord側でロールを作成してください。"
        ]

    lines = [
        f"🎫 「{MEMBER_ROLE_NAME}」ロール：対象{role_result['targets']}人 / "
        f"今回付与{role_result['granted']}人 / 付与済み{role_result['already']}人 / "
        f"失敗{role_result['failed']}人"
    ]
    if role_result["error"]:
        lines.append(f"⚠️ エラー内容：{role_result['error']}")
        lines.append(
            f"（Botの「ロールの管理」権限、Botのロールが「{MEMBER_ROLE_NAME}」より上にあるかを確認してください）"
        )
    return lines


async def _finish_registration(member: discord.Member):
    """
    本名登録が完了したメンバーの制限を解除する。
    「未登録」ロールを外して「会員」ロールを付与し、名前登録チャンネルに
    一時的に付与していた本人専用の閲覧権限も片付ける。
    """
    await _remove_unverified_role(member)
    await _grant_member_role(member)

    register_channel = discord.utils.get(member.guild.text_channels, name=NAME_REGISTER_CHANNEL_NAME)
    if register_channel is not None:
        # 本人宛ての「ようこそ！名前を登録してください」メッセージは、登録が終わったら不要なので削除する
        await _delete_welcome_messages(register_channel, member)
        try:
            await register_channel.set_permissions(member, overwrite=None)
        except discord.Forbidden:
            pass


async def _delete_welcome_messages(channel: discord.TextChannel, member: discord.Member):
    """名前登録チャンネルにある、Botがこのメンバー宛てに送った案内メッセージを削除する。"""
    try:
        async for message in channel.history(limit=200):
            if message.author.id == channel.guild.me.id and member in message.mentions:
                try:
                    await message.delete()
                except discord.HTTPException:
                    pass
    except discord.HTTPException:
        pass


def is_admin():
    """
    管理者コマンドを実行できるかのチェック。
    Discordの「管理者」権限を持っている人に加えて、「管理者」ロール（ADMIN_ROLE_NAME）を
    持っている人も実行できるようにする。
    （「管理者」権限を持つと他人のマッチングルームまで全部見えてしまうため、
      権限は外してロールだけで管理者扱いにする運用に対応するため）
    """

    async def predicate(ctx: commands.Context) -> bool:
        if ctx.guild is None:
            return False
        author = ctx.author
        if author.guild_permissions.administrator:
            return True
        return any(role.name == ADMIN_ROLE_NAME for role in getattr(author, "roles", []))

    return commands.check(predicate)


def run_coro(coro, timeout: int = 15):
    """
    Flask（別スレッド）からBotの非同期処理を呼び出すためのヘルパー。
    Bot側の処理が終わるまでブロックして結果を返す。
    """
    if bot_loop is None:
        raise RuntimeError("Botがまだ起動していません。")
    future = asyncio.run_coroutine_threadsafe(coro, bot_loop)
    return future.result(timeout=timeout)


# --- 旧来の「!setup」コマンド（チャンネル内でボタンを押して即マッチング）も引き続き利用可能 ---
class MatchView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="マッチングする",
        style=discord.ButtonStyle.primary,
        custom_id="persistent_match_button",
    )
    async def match_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        message = interaction.message
        user = interaction.user

        if guild is None or message is None:
            await interaction.followup.send("サーバー内で実行してください。", ephemeral=True)
            return

        if not message.mentions:
            await interaction.followup.send(
                "募集を作成した会員が確認できません。もう一度 !setup を実行してください。",
                ephemeral=True,
            )
            return

        owner = message.mentions[0]

        if user.id == owner.id:
            await interaction.followup.send("自分のボタンは押せません。", ephemeral=True)
            return

        try:
            channel = await match_views.create_match_channel(guild, owner, user)
            await interaction.followup.send(
                f"マッチングルームを作成しました：{channel.mention}", ephemeral=True
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "Botに「チャンネルを管理」の権限がありません。", ephemeral=True
            )
        except discord.HTTPException as error:
            await interaction.followup.send(f"チャンネル作成に失敗しました：{error}", ephemeral=True)


@bot.command()
async def setup(ctx: commands.Context):
    await ctx.send(f"{ctx.author.mention} のマッチング募集👇", view=MatchView())


async def run_id_sync(guild: discord.Guild, send):
    """
    サーバーメンバーのDiscordニックネームとスプレッドシートの「名前（本名）」を
    自動で突き合わせ、一致した分だけDiscordIDをシートに書き込む。

    send: 結果メッセージを送るための非同期関数（ctx.send や channel.send を渡す）。
    「!id同期」コマンドと、新規メンバー参加時の自動実行の両方から呼び出される。
    """
    guild_members = [(str(m.id), m.display_name) for m in guild.members if not m.bot]

    try:
        result = await asyncio.to_thread(sheet_sync.sync_discord_ids, guild_members)
    except Exception as error:  # noqa: BLE001 - 管理者にそのままエラー内容を見せる
        await send(f"エラーが発生しました：{error}")
        return

    lines = [f"✅ {result['matched']}件、DiscordIDを自動入力しました。"]

    if result["ambiguous_names"]:
        lines.append("")
        lines.append("⚠️ 同名のDiscordメンバーが複数いたため、自動判定をスキップしました（手動確認してください）：")
        lines.append("、".join(result["ambiguous_names"]))

    if result["unmatched_sheet_names"]:
        lines.append("")
        lines.append("📋 シートにあるがDiscordで一致しなかった名前：")
        lines.append("、".join(result["unmatched_sheet_names"]))

    if result["unmatched_discord_names"]:
        lines.append("")
        lines.append("👤 Discordにいるがシートで一致しなかった表示名：")
        lines.append("、".join(result["unmatched_discord_names"]))

    # 紐づけが完了している人（今回一致した人＋以前から紐づけ済みだった人）全員に
    # 「会員」ロールを付与する（本番移行時の既存会員への一括付与もここで行われる）
    # 同期処理が特定した「サーバー内にいて紐づけ済み」のメンバーIDをそのまま使う
    role_result = await _grant_member_role_to_linked(guild, result.get("linked_discord_ids"))
    lines.append("")
    lines.extend(_format_role_result(role_result))

    # 「Discordにいるがシートで一致しなかった表示名」がある場合だけ、
    # 管理者ロールにメンションして気づきやすくする
    # （候補が複数・シート側だけの未一致は既存データに大量にあるため対象外にする）
    needs_attention = bool(result["unmatched_discord_names"])
    mention_prefix = ""
    if needs_attention:
        admin_role = discord.utils.get(guild.roles, name=ADMIN_ROLE_NAME)
        if admin_role is not None:
            mention_prefix = f"{admin_role.mention} シートに無いDiscordメンバーがいます。ご確認ください。\n\n"
        else:
            print(f"「{ADMIN_ROLE_NAME}」という名前のロールが見つからないため、メンションできませんでした。")

    body = "\n".join(lines)
    message = mention_prefix + body

    if len(message) <= 1900:
        await send(message)
    else:
        # Discordの1メッセージ2000文字制限を超える場合はテキストファイルで送る
        buffer = io.StringIO(body)
        await send(
            f"{mention_prefix}結果が長くなったのでファイルに出力しました。",
            file=discord.File(fp=buffer, filename="id同期結果.txt"),
        )


@bot.command(name="id同期")
@is_admin()
async def sync_ids(ctx: commands.Context):
    """管理者がチャンネルで「!id同期」と打った時に手動で実行するコマンド。"""
    await ctx.send(
        f"Discordメンバーとスプレッドシートを突き合わせています…（少し時間がかかります）\n"
        f"（bot バージョン：{BOT_VERSION}）"
    )
    await run_id_sync(ctx.guild, ctx.send)


@bot.command(name="未登録ロール設定")
@is_admin()
async def setup_unverified_role(ctx: commands.Context):
    """
    「未登録」ロールから、サーバー内のすべてのチャンネルを見えなくする権限設定を
    まとめて適用する管理者向けコマンド。
    名前登録チャンネルは、本人にだけ参加時に個別で閲覧権限を付与する方式にしているため、
    ロールとしての例外は設けない（＝未登録の人には何も見えない状態になる）。
    チャンネル構成を変えた時は、このコマンドを再実行してください。
    """
    role = discord.utils.get(ctx.guild.roles, name=UNVERIFIED_ROLE_NAME)
    if role is None:
        await ctx.send(f"「{UNVERIFIED_ROLE_NAME}」という名前のロールが見つかりませんでした。先にロールを作成してください。")
        return

    await ctx.send("チャンネル権限を設定しています…（少し時間がかかります）")

    count = 0
    failed = 0
    for channel in ctx.guild.channels:
        try:
            await channel.set_permissions(role, view_channel=False)
            count += 1
        except discord.Forbidden:
            failed += 1

    message = f"✅ {count}件のチャンネルを「{UNVERIFIED_ROLE_NAME}」ロールから見えないようにしました。"
    if failed:
        message += f"\n⚠️ {failed}件は権限不足のため設定できませんでした。"
    await ctx.send(message)


@bot.command(name="未登録ロール一括付与")
@is_admin()
async def bulk_assign_unverified(ctx: commands.Context):
    """
    「会員」ロールが無い既存メンバーに「未登録」ロールをまとめて付ける管理者向けコマンド。
    本番移行時に、すでにサーバーにいるが名前登録が済んでいない人を、
    新規参加者と同じ「名前登録・お知らせだけ見える」状態にそろえるためのもの。
    運営が締め出されないよう、「管理者」ロールや管理者権限を持つ人は対象外にする。
    """
    unverified = discord.utils.get(ctx.guild.roles, name=UNVERIFIED_ROLE_NAME)
    member_role = discord.utils.get(ctx.guild.roles, name=MEMBER_ROLE_NAME)
    if unverified is None or member_role is None:
        await ctx.send(f"「{UNVERIFIED_ROLE_NAME}」または「{MEMBER_ROLE_NAME}」ロールが見つかりません。")
        return

    await ctx.send("会員ロールが無い人に未登録ロールを付けています…（少し時間がかかります）")

    granted = skipped = failed = 0
    for m in ctx.guild.members:
        if m.bot or member_role in m.roles or unverified in m.roles:
            continue
        # 運営は締め出さないよう対象外
        if m.guild_permissions.administrator or any(r.name == ADMIN_ROLE_NAME for r in m.roles):
            skipped += 1
            continue
        try:
            await m.add_roles(unverified, reason="既存メンバーの名前登録誘導のため")
            granted += 1
        except discord.HTTPException:
            failed += 1

    await ctx.send(f"✅ 付与{granted}人 / 運営のため対象外{skipped}人 / 失敗{failed}人")


@bot.command(name="会員ロール設定")
@is_admin()
async def setup_member_role(ctx: commands.Context):
    """
    「会員」ロールを持つ人だけが見られるように、マッチングルーム・承認チャンネル
    （環境変数 MEMBER_ONLY_CHANNEL_NAMES で追加可）の権限をまとめて設定する管理者向けコマンド
    （許可リスト方式：@everyoneを非表示にし、「会員」ロールにだけ閲覧を許可する）。

    あわせて「名前登録」チャンネルも@everyoneから見えないようにする
    （新規参加者には参加時に本人にだけ個別で閲覧権限を付与しているため、
      @everyoneを非表示にしておかないと登録前の人にも全員に見えてしまう）。

    さらに、スプレッドシートと連携済み（＝すでに本名登録済み）の既存会員にも
    「会員」ロールを一括付与する（このロールがないと、切り替え後にマッチングルーム等が
    見えなくなってしまうため）。

    「会員」ロール自体は先にDiscord側で作成しておいてください。
    チャンネル構成を変えた時は、このコマンドを再実行してください。
    """
    role = discord.utils.get(ctx.guild.roles, name=MEMBER_ROLE_NAME)
    if role is None:
        await ctx.send(f"「{MEMBER_ROLE_NAME}」という名前のロールが見つかりませんでした。先にロールを作成してください。")
        return

    await ctx.send("チャンネル権限を設定しています…（少し時間がかかります）")

    count = 0
    failed = 0

    # マッチングルーム・承認などは「会員」ロールだけが見られるようにする
    for channel_name in MEMBER_ONLY_CHANNEL_NAMES:
        channel = discord.utils.get(ctx.guild.text_channels, name=channel_name)
        if channel is None:
            print(f"「{channel_name}」という名前のテキストチャンネルが見つかりませんでした。")
            failed += 1
            continue
        try:
            # 先にBot自身の閲覧・投稿を許可しておく（@everyoneを非表示にした瞬間に
            # Bot自身も見えなくなり、承認メッセージの投稿などが403エラーになるのを防ぐ）
            await channel.set_permissions(ctx.guild.me, **BOT_CHANNEL_PERMISSIONS)
            await channel.set_permissions(ctx.guild.default_role, view_channel=False)
            await channel.set_permissions(role, view_channel=True)
            count += 1
        except discord.HTTPException:
            failed += 1

    # 名前登録チャンネルは@everyoneから見えないようにする
    # （参加時に本人にだけ個別で閲覧権限を付与する方式のため）
    register_channel = discord.utils.get(ctx.guild.text_channels, name=NAME_REGISTER_CHANNEL_NAME)
    if register_channel is not None:
        try:
            await register_channel.set_permissions(ctx.guild.me, **BOT_CHANNEL_PERMISSIONS)
            await register_channel.set_permissions(ctx.guild.default_role, view_channel=False)
            count += 1
        except discord.Forbidden:
            failed += 1
    else:
        print(f"「{NAME_REGISTER_CHANNEL_NAME}」という名前のテキストチャンネルが見つかりませんでした。")
        failed += 1

    await ctx.send("既存の会員（スプレッドシート連携済み）に「会員」ロールを付与しています…")

    role_result = await _grant_member_role_to_linked(ctx.guild)

    message = f"✅ {count}件のチャンネルに権限を設定しました。"
    if failed:
        message += f"\n⚠️ {failed}件のチャンネルは見つからない、または権限不足のため設定できませんでした。"
    message += "\n" + "\n".join(_format_role_result(role_result))
    await ctx.send(message)


@bot.command(name="マッチング解除")
@is_admin()
async def unmatch(ctx: commands.Context, member_a: discord.Member, member_b: discord.Member):
    """
    2人のマッチング成立記録を取り消す（テストのやり直し用）管理者向けコマンド。
    使い方: !マッチング解除 @ユーザー1 @ユーザー2
    ※作成済みのプライベートなマッチングルーム自体は削除されないので、
      不要であれば手動でチャンネルを削除してください。
    """
    match_store.unmark_matched(str(member_a.id), str(member_b.id))

    try:
        removed = await asyncio.to_thread(
            match_records.remove_match, str(member_a.id), str(member_b.id)
        )
    except Exception as error:  # noqa: BLE001 - 管理者にそのままエラー内容を見せる
        await ctx.send(f"エラーが発生しました：{error}")
        return

    if removed:
        await ctx.send(f"✅ {member_a.mention} と {member_b.mention} のマッチング記録を削除しました。")
    else:
        await ctx.send(
            f"ℹ️ 履歴シートには記録が見つかりませんでしたが、"
            f"メモリ上の「マッチング中」の記録は削除しました（{member_a.mention} と {member_b.mention}）。"
        )


@bot.command(name="マッチング復元")
@is_admin()
async def restore_matches(ctx: commands.Context):
    """
    既存の「2人だけが見えるプライベートなテキストチャンネル」をスキャンして、
    マッチング履歴（match_records）にまだ記録されていないペアを書き戻す管理者向けコマンド。
    今回の仕組みを導入する前に成立していたマッチングを拾い直すためのもの。
    """
    await ctx.send("既存のマッチングルームをスキャンしています…（少し時間がかかります）")

    guild = ctx.guild
    found_pairs: set[frozenset] = set()

    for channel in guild.text_channels:
        member_ids = []
        default_denied = False

        for target, overwrite in channel.overwrites.items():
            if target == guild.default_role:
                if overwrite.view_channel is False:
                    default_denied = True
                continue
            if isinstance(target, discord.Member) and not target.bot:
                if overwrite.view_channel is True:
                    member_ids.append(str(target.id))

        # 「@everyoneは見えない」かつ「個人が明示的に2人だけ見える」チャンネルを
        # プライベートなマッチングルームとみなす
        if default_denied and len(member_ids) == 2:
            found_pairs.add(frozenset(member_ids))

    if not found_pairs:
        await ctx.send("それらしいプライベートルームは見つかりませんでした。")
        return

    added = 0
    try:
        existing_pairs = await asyncio.to_thread(match_records.get_all_matched_pairs)
        for pair in found_pairs:
            if pair in existing_pairs:
                continue
            member_a, member_b = tuple(pair)
            await asyncio.to_thread(match_records.append_match, member_a, member_b)
            added += 1
    except Exception as error:  # noqa: BLE001 - 管理者にそのままエラー内容を見せる
        await ctx.send(f"エラーが発生しました：{error}")
        return

    await ctx.send(
        f"✅ プライベートルームを{len(found_pairs)}件検出し、"
        f"うち{added}件を新たにマッチング履歴へ追加しました。"
    )


@bot.command(name="マッチングルーム整理")
@is_admin()
async def organize_match_rooms(ctx: commands.Context):
    """
    既存のマッチングルーム（2人だけが見えるプライベートチャンネル）を、
    専用カテゴリ（MATCH_CATEGORY_NAME）の中へまとめて移動する管理者向けコマンド。
    新しく作られるマッチングルームは最初から専用カテゴリに入るので、実行は最初の1回でOK。
    各ルームの閲覧権限（2人だけが見える設定）はそのまま維持される。
    """
    await ctx.send("既存のマッチングルームを専用カテゴリへ移動しています…（少し時間がかかります）")

    moved = 0
    failed = 0
    for channel in list(ctx.guild.text_channels):
        if channel.category is not None and match_views._is_match_category(channel.category):
            continue  # すでに専用カテゴリ内
        if match_views.is_private_match_room(channel) is None:
            continue  # マッチングルームではない

        try:
            category = await match_views.get_match_category(ctx.guild)
            # sync_permissions=False：カテゴリの権限に上書きされず、2人だけが見える設定を維持する
            await channel.edit(category=category, sync_permissions=False)
            moved += 1
        except discord.HTTPException as error:
            failed += 1
            print(f"「{channel.name}」の移動に失敗しました：{error}")

    message = f"✅ {moved}件のマッチングルームを「{match_views.MATCH_CATEGORY_NAME}」へ移動しました。"
    if failed:
        message += f"\n⚠️ {failed}件は移動に失敗しました（Botの「チャンネルの管理」権限を確認してください）。"
    await ctx.send(message)


class NameModal(discord.ui.Modal, title="本名を登録"):
    """
    「名前を登録する」ボタンを押すと開くフォーム。姓・名を入力してもらい、
    サーバー上のニックネームに反映させたうえでid同期を実行する。
    """

    last_name = discord.ui.TextInput(label="姓", placeholder="例：山田", max_length=20)
    first_name = discord.ui.TextInput(label="名", placeholder="例：太郎", max_length=20)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        guild_id = os.getenv("GUILD_ID")
        if not guild_id:
            await interaction.followup.send(
                "サーバー設定（GUILD_ID）が未設定のため、登録できませんでした。管理者に連絡してください。",
                ephemeral=True,
            )
            return

        guild = bot.get_guild(int(guild_id))
        if guild is None:
            await interaction.followup.send(
                "サーバー情報が取得できませんでした。管理者に連絡してください。",
                ephemeral=True,
            )
            return

        try:
            member = await guild.fetch_member(interaction.user.id)
        except discord.NotFound:
            await interaction.followup.send(
                "サーバーのメンバーとして確認できませんでした。サーバーに参加してから再度お試しください。",
                ephemeral=True,
            )
            return

        # ニックネームの表記は姓名の間に空白を入れない
        full_name = f"{self.last_name.value.strip()}{self.first_name.value.strip()}"

        try:
            await member.edit(nick=full_name)
        except discord.Forbidden:
            await interaction.followup.send(
                "Botに「ニックネームの管理」権限がないため、名前を変更できませんでした。管理者に連絡してください。",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"「{full_name}」として登録しました！会員情報を確認しています…",
            ephemeral=True,
        )

        # すでにスプレッドシートに同名の行があるか、その場で同期を試みる
        channel = discord.utils.get(guild.text_channels, name=ID_SYNC_LOG_CHANNEL_NAME)

        async def send(*args, **kwargs):
            if channel is not None:
                await channel.send(*args, **kwargs)

        await run_id_sync(guild, send)

        profile = sheets_client.find_member(str(member.id), force_refresh=True)

        if profile is not None:
            # 既存の行と連携できたので、これで登録完了
            await _finish_registration(member)
            await interaction.followup.send(
                "既存の会員情報と連携できました！登録は完了です。", ephemeral=True
            )
            return

        # 業種のプルダウン選択肢を取得できないと次のフォームが作れないので、先に確認する
        try:
            business_type_options = sheets_client.get_business_type_options()
        except Exception as error:  # noqa: BLE001 - 本人にそのままエラー内容を見せる
            await interaction.followup.send(
                f"業種の選択肢の取得に失敗しました：{error}\n管理者に連絡してください。",
                ephemeral=True,
            )
            return

        if not business_type_options:
            await interaction.followup.send(
                "業種の選択肢が設定されていないようです。管理者に連絡してください。",
                ephemeral=True,
            )
            return

        # 既存の行が見つからなかった新規会員には、続けてプロフィールを入力してもらう
        await interaction.followup.send(
            "会員情報が見つからなかったため、続けてプロフィールを入力してください。\n"
            "まずは年齢・性別を選んで「次へ」を押してください。",
            ephemeral=True,
            view=ProfileBasicsView(
                member_id=member.id, name=full_name, business_type_options=business_type_options
            ),
        )


AGE_OPTIONS = ["10代", "20代", "30代", "40代", "50代", "60代", "70代以上"]
GENDER_OPTIONS = ["男性", "女性", "その他"]


class ProfileBasicsView(discord.ui.View):
    """
    新規会員のプロフィール入力（1/3）。年齢・性別をプルダウンで選んでもらい、
    「次へ」で都道府県・市町村の入力フォーム（2/3）に進む。
    """

    def __init__(self, member_id: int, name: str, business_type_options: list[str]):
        super().__init__(timeout=900)
        self.member_id = member_id
        self.name = name
        self.business_type_options = business_type_options
        self.selected_age: str | None = None
        self.selected_gender: str | None = None

    @discord.ui.select(
        placeholder="年齢を選択",
        options=[discord.SelectOption(label=age) for age in AGE_OPTIONS],
    )
    async def select_age(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.selected_age = select.values[0]
        await interaction.response.defer()

    @discord.ui.select(
        placeholder="性別を選択",
        options=[discord.SelectOption(label=gender) for gender in GENDER_OPTIONS],
    )
    async def select_gender(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.selected_gender = select.values[0]
        await interaction.response.defer()

    @discord.ui.button(label="次へ", style=discord.ButtonStyle.primary)
    async def next_step(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.member_id:
            await interaction.response.send_message(
                "この登録フォームはあなた宛てではありません。", ephemeral=True
            )
            return

        if not self.selected_age or not self.selected_gender:
            await interaction.response.send_message(
                "年齢と性別の両方を選択してから「次へ」を押してください。", ephemeral=True
            )
            return

        await interaction.response.send_modal(
            ProfileLocationModal(
                member_id=self.member_id,
                name=self.name,
                age=self.selected_age,
                gender=self.selected_gender,
                business_type_options=self.business_type_options,
            )
        )


class ProfileLocationModal(discord.ui.Modal, title="プロフィール入力（続き）"):
    """
    新規会員のプロフィール入力（2/3）。都道府県・市町村を入力してもらい、
    続けて業種・事業内容を入力するフォーム（3/3）に進む。
    """

    prefecture = discord.ui.TextInput(label="都道府県", placeholder="例：北海道", max_length=10)
    city = discord.ui.TextInput(label="市町村", placeholder="例：札幌市", max_length=20)

    def __init__(self, member_id: int, name: str, age: str, gender: str, business_type_options: list[str]):
        super().__init__()
        self.member_id = member_id
        self.name = name
        self.age = age
        self.gender = gender
        self.business_type_options = business_type_options

    async def on_submit(self, interaction: discord.Interaction):
        base_profile = {
            "name": self.name,
            "discord_id": str(self.member_id),
            "age": self.age,
            "gender": self.gender,
            "prefecture": self.prefecture.value.strip(),
            "city": self.city.value.strip(),
        }

        view = BusinessEntryView(
            member_id=self.member_id,
            base_profile=base_profile,
            business_type_options=self.business_type_options,
        )
        await interaction.response.edit_message(content=view.summary_text(), view=view)


class BusinessEntryView(discord.ui.View):
    """
    新規会員のプロフィール入力（3/3）。
    「業種と事業内容を追加」を押すと、業種（プルダウン）と事業/活動内容（文章）を
    1つのフォームでセットで入力できる。複数ある場合は同じボタンで何件でも追加でき、
    「登録を完了する」でスプレッドシートに書き込む（追加した件数だけ行が分かれる）。
    """

    def __init__(self, member_id: int, base_profile: dict, business_type_options: list[str]):
        super().__init__(timeout=900)
        self.member_id = member_id
        self.base_profile = base_profile
        self.business_type_options = business_type_options
        self.businesses: list[dict] = []
        self._refresh_buttons()

    def _refresh_buttons(self):
        # 1件目は「追加」、2件目以降は「もう1件追加」と表示を変えて分かりやすくする
        self.add_business.label = (
            "業種と事業内容を追加する" if not self.businesses else "もう1件、業種と事業内容を追加する"
        )
        self.remove_last.disabled = not self.businesses
        self.finish.disabled = not self.businesses

    def summary_text(self) -> str:
        if not self.businesses:
            listing = "（まだありません）"
        else:
            listing = "\n".join(
                f"{i + 1}. 【{biz['type']}】{biz['content']}" for i, biz in enumerate(self.businesses)
            )
        return (
            "最後に、業種と事業/活動内容を登録します。\n"
            "「業種と事業内容を追加する」を押すと、業種の選択と事業/活動内容の入力を"
            "1つのフォームでまとめて行えます。\n"
            "複数ある場合は、同じように1件ずつ追加してください。\n"
            "すべて入力したら「登録を完了する」を押してください。\n\n"
            f"【登録する業種と事業内容】\n{listing}"
        )

    async def _check_owner(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.member_id:
            await interaction.response.send_message(
                "この登録フォームはあなた宛てではありません。", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="業種と事業内容を追加する", style=discord.ButtonStyle.success, row=0)
    async def add_business(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_owner(interaction):
            return
        await interaction.response.send_modal(BusinessPairModal(parent_view=self))

    @discord.ui.button(label="最後の1件を取り消す", style=discord.ButtonStyle.secondary, row=0)
    async def remove_last(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_owner(interaction):
            return
        if self.businesses:
            self.businesses.pop()
        self._refresh_buttons()
        await interaction.response.edit_message(content=self.summary_text(), view=self)

    @discord.ui.button(label="登録を完了する", style=discord.ButtonStyle.primary, row=1)
    async def finish(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_owner(interaction):
            return

        if not self.businesses:
            await interaction.response.send_message(
                "業種と事業内容を1件も追加していません。少なくとも1件追加してください。",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        # 二重登録防止：この間に別の経路ですでに登録が完了していないか念のため確認する
        existing = sheets_client.find_member(str(self.member_id), force_refresh=True)
        if existing is not None:
            await interaction.edit_original_response(
                content="すでに会員情報が登録されているようです。登録は完了しています。", view=None
            )
            return

        try:
            await asyncio.to_thread(
                sheet_sync.append_new_member_rows, self.base_profile, self.businesses
            )
        except Exception as error:  # noqa: BLE001 - 本人にそのままエラー内容を見せる
            await interaction.edit_original_response(
                content=f"スプレッドシートへの登録に失敗しました：{error}", view=None
            )
            return

        # 直接書き込んだ内容を、キャッシュを待たずにすぐ一覧へ反映させる
        sheets_client.get_members(force_refresh=True)

        guild_id = os.getenv("GUILD_ID")
        if guild_id:
            guild = bot.get_guild(int(guild_id))
            if guild is not None:
                try:
                    member = await guild.fetch_member(self.member_id)
                    await _finish_registration(member)
                except discord.NotFound:
                    pass

        await interaction.edit_original_response(
            content="プロフィールの登録が完了しました！ありがとうございます。", view=None
        )


class BusinessPairModal(discord.ui.Modal, title="業種と事業/活動内容を追加"):
    """
    業種（プルダウン）と事業/活動内容（文章）を1つのフォームでセットで入力してもらう。
    業種の選択肢はスプレッドシートの「設定用」シート「業種1」列から取得した値
    （Discordのプルダウンは最大25個までのため、超える分は先頭25個に絞る）。
    """

    def __init__(self, parent_view: BusinessEntryView):
        super().__init__()
        self.parent_view = parent_view

        self.type_select = discord.ui.Select(
            placeholder="業種を選択してください",
            options=[
                discord.SelectOption(label=option[:100], value=option[:100])
                # 同じ業種が重複しているとDiscord側でエラーになるため重複を除く
                for option in list(dict.fromkeys(o[:100] for o in parent_view.business_type_options))[:25]
            ],
            min_values=1,
            max_values=1,
        )
        self.content_input = discord.ui.TextInput(
            style=discord.TextStyle.paragraph,
            placeholder="例：カフェの経営",
            max_length=200,
        )
        self.add_item(discord.ui.Label(text="業種", component=self.type_select))
        self.add_item(
            discord.ui.Label(
                text="事業/活動内容",
                description="上で選んだ業種で、どんな事業・活動をしているか",
                component=self.content_input,
            )
        )

    async def on_submit(self, interaction: discord.Interaction):
        business_type = self.type_select.values[0] if self.type_select.values else ""
        self.parent_view.businesses.append(
            {"type": business_type, "content": self.content_input.value.strip()}
        )
        self.parent_view._refresh_buttons()
        await interaction.response.edit_message(
            content=self.parent_view.summary_text(), view=self.parent_view
        )


class NameRegisterView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="名前を登録する",
        style=discord.ButtonStyle.primary,
        custom_id="persistent_name_register_button",
    )
    async def register(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(NameModal())


async def _report_interaction_error(interaction: discord.Interaction, error: Exception):
    """登録フォーム・ボタンで想定外のエラーが起きた時に、黙って止まらず本人に知らせる。"""
    import traceback

    traceback.print_exception(type(error), error, error.__traceback__)
    message = f"処理中にエラーが発生しました：{error}\nお手数ですが管理者に連絡してください。"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass


async def _modal_on_error(self, interaction: discord.Interaction, error: Exception):
    await _report_interaction_error(interaction, error)


async def _view_on_error(self, interaction: discord.Interaction, error: Exception, item):
    await _report_interaction_error(interaction, error)


for _modal_cls in (NameModal, ProfileLocationModal, BusinessPairModal):
    _modal_cls.on_error = _modal_on_error
for _view_cls in (ProfileBasicsView, BusinessEntryView, NameRegisterView):
    _view_cls.on_error = _view_on_error


@bot.command(name="登録ボタン設置")
@is_admin()
async def post_register_button(ctx: commands.Context):
    """
    打ったチャンネルに「名前を登録する」ボタンを設置する管理者向けコマンド
    （既存メンバーの名前登録誘導用。名前登録チャンネルで打つ想定）。
    ボタンは永続化されているので、Botが再起動しても反応し続ける。
    """
    try:
        await ctx.message.delete()  # コマンドのメッセージ自体は消す
    except discord.HTTPException:
        pass
    await ctx.send(
        "📝 本名の登録がまだの方は、下のボタンから登録してください。\n"
        "登録が終わると「会員」ロールが付き、マッチング機能が使えるようになります。",
        view=NameRegisterView(),
    )


@bot.event
async def on_member_join(member: discord.Member):
    """
    新しいメンバーがサーバーに参加したら、
    1. 「未登録」ロールを付与する
    2. 「名前登録」チャンネルへの閲覧権限を本人にだけ一時的に付与し、
       そのチャンネル内でメンション付きの案内メッセージを送る（DMは使わない）

    参加直後の自動id同期は行わない（id同期ログが毎回動いてしまうのを避けるため）。
    自動同期は、ニックネームを変更した時（on_member_update）にのみ実行される。
    """
    guild = member.guild

    unverified_role = discord.utils.get(guild.roles, name=UNVERIFIED_ROLE_NAME)
    if unverified_role is not None:
        try:
            await member.add_roles(unverified_role, reason="本名登録が完了するまでの制限用")
        except discord.Forbidden:
            print(f"「{UNVERIFIED_ROLE_NAME}」ロールを付与する権限がBotにありません。")
    else:
        print(f"「{UNVERIFIED_ROLE_NAME}」という名前のロールが見つかりませんでした。")

    register_channel = discord.utils.get(guild.text_channels, name=NAME_REGISTER_CHANNEL_NAME)
    if register_channel is not None:
        try:
            await register_channel.set_permissions(
                member, view_channel=True, send_messages=True, read_message_history=True
            )
        except discord.Forbidden:
            print(f"「{NAME_REGISTER_CHANNEL_NAME}」チャンネルの権限設定に失敗しました。")

        await register_channel.send(
            f"🎉 {member.mention} さん、ようこそ！\n"
            "会員情報と連携するために、まずは本名を登録してください。",
            view=NameRegisterView(),
        )
    else:
        print(f"「{NAME_REGISTER_CHANNEL_NAME}」という名前のテキストチャンネルが見つかりませんでした。")


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    """
    メンバーのニックネームが変更されたら、自動で「id同期」を実行する。
    名前登録フォーム経由の変更・本人がDiscordの設定から手動で変更した場合の
    どちらも検知する（ロールの変更など、ニックネーム以外の更新は対象外）。
    """
    if before.nick == after.nick:
        return

    guild = after.guild
    channel = discord.utils.get(guild.text_channels, name=ID_SYNC_LOG_CHANNEL_NAME)

    async def send(*args, **kwargs):
        if channel is not None:
            await channel.send(*args, **kwargs)
        else:
            print(f"「{ID_SYNC_LOG_CHANNEL_NAME}」チャンネルが見つからないため、id同期の結果を送信できませんでした。")

    if channel is not None:
        await channel.send(
            f"✏️ {after.mention} さんのニックネームが変更されました。DiscordIDの自動突き合わせを行います…"
        )

    await run_id_sync(guild, send)


async def post_matching_room_link():
    """
    「マッチングルーム」チャンネルに、会員情報確認・マッチング申請ページの
    固定URLを投稿する。すでに投稿済み（直近の履歴にBot自身の同じURL投稿がある）
    場合は再投稿しない（Railway再起動のたびにスパムしないため）。
    """
    guild_id = os.getenv("GUILD_ID")

    if not guild_id or not WEB_APP_URL:
        print("GUILD_ID または WEB_APP_URL が未設定のため、リンク投稿をスキップしました。")
        return

    guild = bot.get_guild(int(guild_id))
    if guild is None:
        print("マッチングルームへの投稿に失敗：Botが指定サーバーに参加していません。")
        return

    channel = discord.utils.get(guild.text_channels, name=MATCHING_ROOM_CHANNEL_NAME)
    if channel is None:
        print(f"「{MATCHING_ROOM_CHANNEL_NAME}」という名前のテキストチャンネルが見つかりませんでした。")
        return

    async for message in channel.history(limit=20):
        if message.author.id == bot.user.id and WEB_APP_URL in message.content:
            return  # すでに投稿済み

    await channel.send(f"🔗 会員情報の確認・マッチング申請はこちらから！\n{WEB_APP_URL}")


@bot.event
async def on_ready():
    global bot_loop
    bot_loop = asyncio.get_running_loop()

    # Railway再起動後も既存ボタンを反応させる
    bot.add_view(MatchView())
    bot.add_view(NameRegisterView())
    print(f"ログインしました: {bot.user}")

    await post_matching_room_link()


def start_web_server():
    app = web_app.create_app(bot, run_coro)
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)


token = os.getenv("TOKEN")

if not token:
    raise RuntimeError("RailwayのVariablesにTOKENが設定されていません。")

# WebサーバーはBotとは別スレッドで動かす（Botのループはメインスレッドで動かし続ける）
web_thread = threading.Thread(target=start_web_server, daemon=True)
web_thread.start()

bot.run(token)
