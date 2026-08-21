"""
マッチング申請の承認・拒否をDM上で行うためのViewと、
プライベートなマッチングルーム作成処理をまとめたモジュール。
"""

import os
import discord

import match_store

GUILD_ID = os.getenv("GUILD_ID")


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
    return channel


class MatchApproveView(discord.ui.View):
    """
    DMで送られる「承認する / 断る」ボタン。
    request_id を持たせておき、押されたタイミングで match_store から
    申請内容（誰から誰への申請か）を引く。
    """

    def __init__(self, bot: discord.Client, request_id: str):
        # 承認待ちのままDMに残っていてもよいようタイムアウトなしにしている
        super().__init__(timeout=None)
        self.bot = bot
        self.request_id = request_id

    async def _disable_all(self, interaction: discord.Interaction):
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)

    @discord.ui.button(label="承認する", style=discord.ButtonStyle.success, custom_id="match_approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        request = match_store.resolve_request(self.request_id)
        await self._disable_all(interaction)

        if request is None:
            await interaction.followup.send(
                "この申請はすでに期限切れ、またはキャンセルされています。",
                ephemeral=True,
            )
            return

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

        try:
            await requester.send(
                f"{target.display_name} さんがマッチング申請を承認しました！ {channel.mention} をご確認ください。"
            )
        except discord.Forbidden:
            pass  # DMを閉じているユーザーには通知できない

    @discord.ui.button(label="断る", style=discord.ButtonStyle.danger, custom_id="match_reject")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        request = match_store.resolve_request(self.request_id)
        await self._disable_all(interaction)

        await interaction.followup.send("申請を断りました。", ephemeral=True)

        if request is None or not GUILD_ID:
            return

        guild = self.bot.get_guild(int(GUILD_ID))
        if guild is None:
            return

        try:
            requester = await guild.fetch_member(int(request["requester_id"]))
            await requester.send("残念ながら、マッチング申請は今回見送られました。")
        except (discord.NotFound, discord.Forbidden):
            pass


async def send_match_request_dm(bot: discord.Client, requester_id: str, target_id: str, request_id: str):
    """Webページからのマッチング申請を、相手にDMで通知する。"""
    if not GUILD_ID:
        raise RuntimeError("環境変数 GUILD_ID が設定されていません。")

    guild = bot.get_guild(int(GUILD_ID))
    if guild is None:
        raise RuntimeError("Botが指定のサーバーに参加していないか、GUILD_IDが誤っています。")

    requester = await guild.fetch_member(int(requester_id))
    target = await guild.fetch_member(int(target_id))

    view = MatchApproveView(bot, request_id)
    await target.send(
        f"💌 **{requester.display_name}** さんからマッチング希望です！マッチングしますか？",
        view=view,
    )
