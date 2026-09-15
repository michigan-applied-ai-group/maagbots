"""The MAAG Discord bot. Run it with: python bot.py

One bot account speaks for every agent in agents/. Each agent posts through a
Discord webhook, which lets a single message use any name and avatar. That's
how 15 personas can share one bot instead of needing 15 bot accounts.
"""

import logging
import os
import time

import anthropic
import discord
from dotenv import load_dotenv

from loader import Agent, load_agents

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("maagbots")

MODEL = "claude-opus-5"
HISTORY_LIMIT = 20          # how many recent messages an agent reads before replying
MAX_HOPS = 2                # agent-to-agent chains stop after this many replies
COOLDOWN_SECONDS = 60       # after an agent replies, the channel rests this long
QUIET_SECONDS = 30 * 60     # how long `!quiet` silences every agent
DISCORD_MAX_LENGTH = 2000   # Discord rejects longer messages

# Discord only sends message text to bots that ask for the "message content"
# intent. It also has to be switched on in the Developer Portal.
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
claude = anthropic.AsyncAnthropic()

agents: list[Agent] = []
webhooks: dict[int, discord.Webhook] = {}   # channel id -> our webhook there
hops: dict[int, int] = {}                   # id of a message an agent sent -> its hop count
cooldown_until: dict[int, float] = {}       # channel id -> time the cooldown ends
quiet_until: dict[int, float] = {}          # channel id -> time `!quiet` ends


def pick_agent(message: discord.Message) -> Agent | None:
    """Return the one agent that should answer this message, or None."""
    text = message.content.lower()
    for agent in agents:
        if message.channel.name not in agent.channels:
            continue
        if agent.name == message.author.name:
            continue  # an agent never answers itself
        # Personas aren't real Discord users, so they can't be @-mentioned the
        # normal way. Typing "@The Skeptic" in plain text counts as a mention.
        if agent.trigger == "mention" and f"@{agent.name.lower()}" in text:
            return agent
    return None


async def get_webhook(channel: discord.TextChannel) -> discord.Webhook:
    """Find this bot's webhook in the channel, creating it the first time."""
    if channel.id not in webhooks:
        existing = [w for w in await channel.webhooks() if w.user == client.user]
        webhooks[channel.id] = existing[0] if existing else await channel.create_webhook(name="maagbots")
    return webhooks[channel.id]


async def write_reply(agent: Agent, message: discord.Message) -> str | None:
    """Ask Claude to reply as the agent. Returns None if there's nothing to say."""
    history = [m async for m in message.channel.history(limit=HISTORY_LIMIT)]
    transcript = "\n".join(
        f"{m.author.display_name}: {m.content}" for m in reversed(history)
    )
    prompt = (
        f"Recent messages in #{message.channel.name}, oldest first:\n\n"
        f"{transcript}\n\n"
        f"{message.author.display_name} just mentioned you in the last message. "
        "Write your reply. Just the reply, no name prefix."
    )

    try:
        # If a safety check declines the request, `fallbacks` lets the API
        # retry it on another model instead of returning nothing.
        response = await claude.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=agent.system_prompt,
            messages=[{"role": "user", "content": prompt}],
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.APIError as e:
        log.error("Claude API error for %s: %s", agent.name, e)
        return None

    if response.stop_reason == "refusal":
        log.warning("%s declined to reply to message %s", agent.name, message.id)
        return None
    text = "".join(block.text for block in response.content if block.type == "text").strip()
    return text[:DISCORD_MAX_LENGTH] or None


@client.event
async def on_ready():
    log.info("Logged in as %s with %d agent(s)", client.user, len(agents))


@client.event
async def on_message(message: discord.Message):
    channel = message.channel
    if not isinstance(channel, discord.TextChannel):
        return  # ignore DMs and threads for now
    now = time.monotonic()

    # The kill switch. Anyone can use it, no permissions check.
    if message.content.strip() == "!quiet":
        quiet_until[channel.id] = now + QUIET_SECONDS
        await channel.send("🤫 The agents will stay quiet for 30 minutes.")
        return

    # Work out how deep into an agent-to-agent chain this message is.
    # A human message is hop 0. Messages from other bots are ignored.
    if message.webhook_id and message.id in hops:
        hop = hops[message.id]
    elif message.author.bot:
        return
    else:
        hop = 0

    if now < quiet_until.get(channel.id, 0):
        return
    if hop >= MAX_HOPS:
        return  # the chain has gone far enough
    if hop == 0 and now < cooldown_until.get(channel.id, 0):
        return  # cooling down: stay silent rather than queueing

    agent = pick_agent(message)
    if agent is None:
        return

    # Start the cooldown now, so a second message arriving while Claude is
    # thinking doesn't get its own reply.
    cooldown_until[channel.id] = now + COOLDOWN_SECONDS

    reply = await write_reply(agent, message)
    if reply is None:
        return

    webhook = await get_webhook(channel)
    sent = await webhook.send(
        reply,
        username=agent.name,
        avatar_url=agent.avatar or None,
        wait=True,  # wait for Discord to return the message, so we learn its id
    )
    hops[sent.id] = hop + 1


if __name__ == "__main__":
    agents = load_agents()
    client.run(os.environ["DISCORD_TOKEN"], log_handler=None)
