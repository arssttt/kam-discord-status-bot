from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import discord


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("kam-status-bot")


@dataclass(frozen=True)
class Settings:
    token: str
    channel_id: int
    poller_path: str
    game_revision: str
    master_url: str
    include_empty_rooms: bool
    poller_timeout: str
    update_interval: int
    error_retry_interval: int
    message_file: Path
    activity: str

    @classmethod
    def from_env(cls) -> "Settings":
        token = require_env("DISCORD_TOKEN")
        channel_id = int(require_env("DISCORD_CHANNEL_ID"))
        return cls(
            token=token,
            channel_id=channel_id,
            poller_path=os.getenv("POLLER_PATH", "./server-poller-json-linux-amd64"),
            game_revision=os.getenv("GAME_REVISION", "r16020"),
            master_url=os.getenv("MASTER_URL", "http://master.kamremake.com/"),
            include_empty_rooms=parse_bool(os.getenv("INCLUDE_EMPTY_ROOMS", "false")),
            poller_timeout=os.getenv("POLLER_TIMEOUT", "6s"),
            update_interval=int(os.getenv("UPDATE_INTERVAL", "60")),
            error_retry_interval=int(os.getenv("ERROR_RETRY_INTERVAL", "30")),
            message_file=Path(os.getenv("STATUS_MESSAGE_FILE", "/app/data/status-message.json")),
            activity=os.getenv("BOT_ACTIVITY", "KaM server status"),
        )


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Environment variable {name} is required")
    return value


def parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def poller_command(settings: Settings) -> list[str]:
    command = [
        settings.poller_path,
        "-gameRevision",
        settings.game_revision,
        "-master",
        settings.master_url,
        "-timeout",
        settings.poller_timeout,
    ]
    if settings.include_empty_rooms:
        command.append("-includeEmptyRooms")
    return command


async def run_poller(settings: Settings) -> dict[str, Any]:
    command = poller_command(settings)
    logger.info("Running poller: %s", " ".join(command))

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()

    if process.returncode != 0:
        error_text = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"poller exited with code {process.returncode}: {error_text}")

    raw = stdout.decode("utf-8", errors="replace").strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        preview = raw[:500] if raw else "<empty stdout>"
        raise RuntimeError(f"poller returned invalid JSON: {exc}; stdout={preview!r}") from exc

    validate_payload(payload)
    return payload


def validate_payload(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise ValueError("JSON root must be an object")
    if not isinstance(payload.get("RoomCount"), int):
        raise ValueError("RoomCount must be an integer")
    rooms = payload.get("Rooms")
    if not isinstance(rooms, list):
        raise ValueError("Rooms must be a list")
    for index, room in enumerate(rooms):
        if not isinstance(room, dict):
            raise ValueError(f"Rooms[{index}] must be an object")
        if not isinstance(room.get("Server"), dict):
            raise ValueError(f"Rooms[{index}].Server must be an object")
        if not isinstance(room.get("GameInfo"), dict):
            raise ValueError(f"Rooms[{index}].GameInfo must be an object")


def clean(value: Any, fallback: str = "-") -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    return text if text else fallback


def room_sort_key(room: dict[str, Any]) -> tuple[int, int, int]:
    info = room.get("GameInfo", {})
    server = room.get("Server", {})
    state = str(info.get("GameState") or "")
    players = count_online_players(info.get("Players", []))
    ping = int(server.get("Ping") or 9999)
    lobby_rank = 0 if state == "mgsLobby" else 1
    return (lobby_rank, -players, ping)


def summarize(payload: dict[str, Any]) -> dict[str, int]:
    rooms = payload.get("Rooms", [])
    total_players = 0
    playing_rooms = 0
    locked_rooms = 0
    for room in rooms:
        info = room.get("GameInfo", {})
        players = count_online_players(info.get("Players", []))
        total_players += players
        if players > 0:
            playing_rooms += 1
        if info.get("PasswordLocked"):
            locked_rooms += 1
    return {
        "rooms": len(rooms),
        "players": total_players,
        "playing_rooms": playing_rooms,
        "locked_rooms": locked_rooms,
    }


def format_game_state(value: Any) -> str:
    states = {
        "mgsNone": "Idle",
        "mgsLobby": "Lobby",
        "mgsLoading": "Loading",
        "mgsGame": "In game",
        "mgsGameOver": "Finished",
    }
    return states.get(str(value), clean(value, "Unknown"))


def status_title(game_revision: str) -> str:
    return f"🛡️ KaM Remake Status {game_revision}"


def build_embeds(payload: dict[str, Any], game_revision: str) -> list[discord.Embed]:
    summary = summarize(payload)
    rooms = sorted(payload.get("Rooms", []), key=room_sort_key)
    now = datetime.now(timezone.utc)

    description = (
        f"👥 **{summary['players']}** players online | 🏰 **{summary['rooms']}** rooms | "
        f"⚔️ **{summary['playing_rooms']}** active games"
    )
    if summary["locked_rooms"]:
        description += f" | 🔒 **{summary['locked_rooms']}** locked"

    header = discord.Embed(
        title=status_title(game_revision),
        description=description,
        color=discord.Color.from_rgb(76, 175, 123),
        timestamp=now,
    )
    header.set_footer(text="Updates automatically")

    if not rooms:
        header.add_field(name="🏰 Rooms", value="No public rooms right now.", inline=False)
        return [header]

    embeds = [header]
    for index, room in enumerate(rooms[:9], start=1):
        embeds.append(build_room_embed(room, index))

    if len(rooms) > 9:
        header.add_field(name="➕ More rooms", value=f"And {len(rooms) - 9} more room(s).", inline=False)

    return embeds


def build_room_embed(room: dict[str, Any], index: int) -> discord.Embed:
    server = room.get("Server", {})
    info = room.get("GameInfo", {})
    options = info.get("GameOptions", {})
    players = info.get("Players", [])
    active_players = [p for p in players if not p.get("IsSpectator")]
    spectators = [p for p in players if p.get("IsSpectator")]
    occupied_slots = count_occupied_slots(players)

    name = clean(server.get("Name"), "Unnamed server")
    endpoint = f"{clean(server.get('IP'))}:{clean(server.get('Port'))}"
    lock = " 🔒" if info.get("PasswordLocked") else ""
    title = f"{status_dot(info.get('GameState'))} {name}#{index}{lock} ({endpoint})"
    speeds = (
        f"{clean(options.get('Peacetime'))}pt "
        f"x{clean(options.get('SpeedPT'))} "
        f"x{clean(options.get('SpeedAfterPT'))}"
    )

    embed = discord.Embed(
        title=clip(title, 256),
        description=f"**{format_game_state(info.get('GameState'))}** | ⏱️ {clean(info.get('GameTime'))}",
        color=room_color(info.get("GameState")),
    )
    embed.add_field(name="🗺️ Map", value=clip(clean(info.get("Map")), 1024), inline=True)
    embed.add_field(name="⚙️ Options", value=speeds, inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)
    description = clean(info.get("Description"), "")
    if description:
        embed.add_field(name="📝 Description", value=clip(description, 1024), inline=False)
    embed.add_field(
        name=f"👥 Players ({occupied_slots}/12)",
        value=clip(format_players(active_players, spectators), 1024),
        inline=False,
    )
    return embed


def status_dot(value: Any) -> str:
    if value == "mgsLobby":
        return "🟡"
    if value == "mgsGame":
        return "🟢"
    if value == "mgsGameOver":
        return "🔴"
    return "⚪"


def room_color(value: Any) -> discord.Color:
    if value == "mgsLobby":
        return discord.Color.from_rgb(229, 180, 84)
    if value == "mgsGame":
        return discord.Color.from_rgb(92, 190, 112)
    if value == "mgsGameOver":
        return discord.Color.from_rgb(214, 85, 70)
    return discord.Color.from_rgb(120, 132, 140)


def format_players(active_players: list[dict[str, Any]], spectators: list[dict[str, Any]]) -> str:
    closed_slots = [player for player in active_players if player_type(player) == "nptClosed"]
    playable_players = [player for player in active_players if player_type(player) != "nptClosed"]

    if not playable_players and not spectators and not closed_slots:
        return "No players listed."

    teams: dict[int, list[str]] = {}
    for player in playable_players:
        team = int(player.get("Team") or 0)
        teams.setdefault(team, []).append(format_player(player))

    team_ids = {team for team in teams if team > 0}
    show_teams = len(team_ids) > 1

    chunks: list[str] = []
    for team, names in sorted(teams.items()):
        team_label = team_badge(team) if show_teams else "⚔️"
        chunks.append(f"{team_label} {', '.join(names[:8])}")

    if spectators:
        names = [format_player(player) for player in spectators[:10]]
        chunks.append(f"👁️ **Spectators:** {', '.join(names)}")

    if closed_slots:
        chunks.append(f"🔒 **Closed slots:** {len(closed_slots)}")

    return "\n".join(chunks)


def format_player(player: dict[str, Any]) -> str:
    player_type = clean(player.get("PlayerType"), "nptHuman")
    is_bot = player_type in {"nptComputerClassic", "nptComputerAdvanced"}
    name = player_type_label(player_type) if is_bot else clean(player.get("Name"), "Unknown")
    markers = []
    if player.get("IsHost"):
        markers.append("👑")
    if is_bot:
        markers.append("🤖 BOT")

    suffix = f" ({', '.join(markers)})" if markers else ""
    return f"{lang_flag(player.get('LangCode'))} {color_square(player.get('Color'))} **{name}**{suffix}"


def count_online_players(players: Any) -> int:
    if not isinstance(players, list):
        return 0
    return sum(1 for player in players if player_type(player) != "nptClosed")


def count_occupied_slots(players: Any) -> int:
    if not isinstance(players, list):
        return 0
    return len(players)


def player_type(player: Any) -> str:
    if not isinstance(player, dict):
        return "nptClosed"
    return clean(player.get("PlayerType"), "nptHuman")


def player_type_label(value: str) -> str:
    labels = {
        "nptHuman": "Human",
        "nptClosed": "Closed",
        "nptComputerClassic": "AI",
        "nptComputerAdvanced": "AdvAI",
    }
    return labels.get(value, value)


def team_badge(team: int) -> str:
    return f"**Team {team}:**" if team else "**FFA:**"


def lang_flag(lang_code: Any) -> str:
    flags = {
        "rus": "🇷🇺",
        "eng": "🇬🇧",
        "pol": "🇵🇱",
        "slv": "🇸🇮",
        "slo": "🇸🇮",
        "ger": "🇩🇪",
        "deu": "🇩🇪",
        "fra": "🇫🇷",
        "fre": "🇫🇷",
        "spa": "🇪🇸",
        "esp": "🇪🇸",
        "ita": "🇮🇹",
        "cze": "🇨🇿",
        "ces": "🇨🇿",
        "ukr": "🇺🇦",
        "bel": "🇧🇾",
        "bra": "🇧🇷",
        "brz": "🇧🇷",
        "ptb": "🇧🇷",
        "por": "🇵🇹",
        "chi": "🇨🇳",
        "chn": "🇨🇳",
        "zho": "🇨🇳",
        "jpn": "🇯🇵",
        "kor": "🇰🇷",
        "tur": "🇹🇷",
        "hun": "🇭🇺",
        "dut": "🇳🇱",
        "nld": "🇳🇱",
        "swe": "🇸🇪",
        "fin": "🇫🇮",
    }
    return flags.get(str(lang_code).lower(), "🏳️")


def color_square(color: Any) -> str:
    value = clean(color, "#808080").lstrip("#")
    if len(value) != 6:
        return "⬜"
    try:
        red = int(value[0:2], 16)
        green = int(value[2:4], 16)
        blue = int(value[4:6], 16)
    except ValueError:
        return "⬜"

    if red < 45 and green < 45 and blue < 45:
        return "⬛"
    if red > 210 and green > 210 and blue > 210:
        return "⬜"
    if abs(red - green) < 35 and abs(red - blue) < 35 and abs(green - blue) < 35:
        return "⬜"
    if red >= green and red >= blue:
        return "🟧" if green > 90 else "🟥"
    if green >= red and green >= blue:
        return "🟩"
    return "🟦"


def build_error_embeds(error: Exception, retry_seconds: int, game_revision: str) -> list[discord.Embed]:
    embed = discord.Embed(
        title=status_title(game_revision),
        description=(
            "⚠️ Status data is temporarily unavailable.\n"
            f"🔁 Next poll in **{retry_seconds}** seconds."
        ),
        color=discord.Color.from_rgb(214, 85, 70),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Poller error", value=clip(str(error), 1024), inline=False)
    embed.set_footer(text="The bot will retry automatically")
    return [embed]


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "..."


def read_message_id(path: Path) -> int | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        message_id = data.get("message_id")
        return int(message_id) if message_id else None
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        logger.warning("Cannot read status message file %s: %s", path, exc)
        return None


def write_message_id(path: Path, message_id: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"message_id": message_id}, indent=2), encoding="utf-8")


class StatusBot(discord.Client):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.settings = settings
        self.status_message: discord.Message | None = None

    async def setup_hook(self) -> None:
        self.loop.create_task(self.status_loop())

    async def on_ready(self) -> None:
        logger.info("Logged in as %s", self.user)
        await self.change_presence(activity=discord.Game(name=self.settings.activity))

    async def status_loop(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            delay = self.settings.update_interval
            try:
                payload = await run_poller(self.settings)
                await self.publish_status(payload=payload, error=None)
            except Exception as exc:
                logger.exception("Status update failed")
                delay = self.settings.error_retry_interval
                await self.publish_status(payload=None, error=exc)
            await asyncio.sleep(delay)

    async def publish_status(self, payload: dict[str, Any] | None, error: Exception | None) -> None:
        channel = await self.fetch_channel(self.settings.channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            raise RuntimeError(f"Channel {self.settings.channel_id} cannot receive messages")

        embeds = (
            build_error_embeds(error, self.settings.error_retry_interval, self.settings.game_revision)
            if error is not None or payload is None
            else build_embeds(payload, self.settings.game_revision)
        )

        message = await self.get_status_message(channel)
        if message is None:
            message = await channel.send(embeds=embeds)
            self.status_message = message
            write_message_id(self.settings.message_file, message.id)
            logger.info("Created status message %s", message.id)
            return

        await message.edit(embeds=embeds, attachments=[])
        self.status_message = message

    async def get_status_message(
        self,
        channel: discord.abc.Messageable,
    ) -> discord.Message | None:
        if self.status_message is not None:
            return self.status_message

        message_id = read_message_id(self.settings.message_file)
        if message_id is None:
            return None

        try:
            return await channel.fetch_message(message_id)
        except discord.NotFound:
            logger.warning("Stored status message %s was not found; creating a new one", message_id)
            return None


def main() -> None:
    settings = Settings.from_env()
    client = StatusBot(settings)
    client.run(settings.token)


if __name__ == "__main__":
    main()
