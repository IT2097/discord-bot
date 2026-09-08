"""
マッチング申請の承認・拒否を「承認」チャンネルで行うためのViewと、
プライベートなマッチングルーム作成処理をまとめたモジュール。
"""

import os
import asyncio
import discord

import match_store
import match_records
import sheets_client

GUILD_ID = os.getenv("GUILD_ID")

# マッチング申請の承認メッセージを投稿するテキストチャンネル名
APPROVAL_CHANNEL_NAME = os.getenv("APPROVAL_CHANNEL_NAME", "承認")


def _format_profile(member: dict) -> str:
    """
    スプレッドシートのプロフィール情報（名前・年齢・性別・都道府県・市町村・
    業種・事業/活動内容）を、DMやチャンネル投稿用のテキストに整形する。
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

    channel = await guild.create_text_channel(
        name=f"{member_a.display_name}-{member_b.display_name}",
        overwrites=overwrites,
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


class MatchApproveView(discord.ui.View):
    """
    「承認」チャンネルに投稿される「承認する / 断る」ボタン。
    request_id を持たせておき、押されたタイミングで match_store から
    申請内容（誰から誰への申請か）を引く。

    チャンネルに投稿されるため、押せるのは申請の宛先本人のみに制限する。
    """

    def __init__(self, bot: discord.Client, request_id: str):
        # 承認待ちのままチャンネルに残っていてもよいようタイムアウトなしにしている
        super().__init__(timeout=None)
        self.bot = bot
        self.request_id = request_id

    async def _disable_all(self, interaction: discord.Interaction):
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)

    async def _check_is_target(self, interaction: discord.Interaction, request: dict) -> bool:
        if str(interaction.user.id) != request["target_id"]:
            await interaction.response.send_message(
                "この申請はあなた宛てではないため、操作できません。", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="承認する", style=discord.ButtonStyle.success, custom_id="match_approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        request = match_store.get_request(self.request_id)

        if request is None:
            await interaction.response.send_message(
                "この申請はすでに期限切れ、またはキャンセルされています。",
                ephemeral=True,
            )
            return

        if not await self._check_is_target(interaction, request):
            return

        await interaction.response.defer()
        match_store.resolve_request(self.request_id)
        await self._disable_all(interaction)

        if not GUILD_ID:
            await interaction.followup.send(
                "サーバー設定（GUILD_ID）が未設定のため、ルームを作成できませんでした。管理者に連絡してください。",
                ephemeral=True,
            )
            return

        guild = self.bot.get_guild(int(GUILD_ID))
        if guild is None:
            await interaction.followup.send(
                "サーバー情報が取得できませんでした。管理者に連絡してください。",
                ephemeral=True,
            )
            return

        try:
            requester = await guild.fetch_member(int(request["requester_id"]))
            target = await guild.fetch_member(int(request["target_id"]))
        except discord.NotFound:
            await interaction.followup.send(
                "相手がサーバーに見つかりませんでした。すでに退会された可能性があります。",
                ephemeral=True,
            )
            return

        try:
            channel = await create_match_channel(guild, requester, target)
        except discord.Forbidden:
            await interaction.followup.send(
                "Botに「チャンネルを管理」の権限がありません。管理者に連絡してください。",
                ephemeral=True,
            )
            return
        except discord.HTTPException as error:
            await interaction.followup.send(f"チャンネル作成に失敗しました：{error}", ephemeral=True)
            return

        await interaction.followup.send(
            f"マッチングが成立しました！ {channel.mention} をご確認ください。",
            ephemeral=True,
        )

        match_store.mark_matched(request["requester_id"], request["target_id"])

        try:
            # スプレッドシートへの書き込みはブロッキング処理なので別スレッドで実行する
            await asyncio.to_thread(
                match_records.append_match, request["requester_id"], request["target_id"]
            )
        except Exception:  # noqa: BLE001 - 永続化に失敗してもマッチング自体は成立させる
            print("マッチング履歴の永続化に失敗しました。")

        try:
            await requester.send(
                f"{target.display_name} さんがマッチング申請を承認しました！ {channel.mention} をご確認ください。"
            )
        except discord.Forbidden:
            pass  # DMを閉じているユーザーには通知できない

    @discord.ui.button(label="断る", style=discord.ButtonStyle.danger, custom_id="match_reject")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        request = match_store.get_request(self.request_id)

        if request is None:
            await interaction.response.send_message(
                "この申請はすでに期限切れ、またはキャンセルされています。",
                ephemeral=True,
            )
            return

        if not await self._check_is_target(interaction, request):
            return

        await interaction.response.defer()
        match_store.resolve_request(self.request_id)
        await self._disable_all(interaction)

        await interaction.followup.send("申請を断りました。", ephemeral=True)

        if not GUILD_ID:
            return

        guild = self.bot.get_guild(int(GUILD_ID))
        if guild is None:
            return

        try:
            requester = await guild.fetch_member(int(request["requester_id"]))
            await requester.send("残念ながら、マッチング申請は今回見送られました。")
        except (discord.NotFound, discord.Forbidden):
            pass


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

    view = MatchApproveView(bot, request_id)
    await channel.send(
        f"{target.mention}\n{requester.display_name} さんからマッチの依頼が来ました！承認しますか？",
        view=view,
    )
