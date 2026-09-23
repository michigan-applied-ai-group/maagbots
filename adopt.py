"""Turns students' agent descriptions, posted in a Discord thread, into agent files.

Students reply to the handout (docs/aspect-agent-handout.md) with a filled-in
template. `!adopt` in that thread sends each reply to Claude, which writes the
system prompt. The fields a student shouldn't have to think about (owner,
channels, trigger) are filled in here, not by Claude.

Like evolve.py, this never writes to agents/. The files come back as
attachments, and a human opens the pull request.
"""

import asyncio
import logging
import re
from dataclasses import dataclass

import yaml

from loader import AGENTS_DIR, AgentFileError, parse_agent_text

log = logging.getLogger("maagbots.adopt")

MESSAGES_TO_READ = 200      # how far back in the thread to look for replies
SKIP = "SKIP"               # what Claude says when a message isn't a description

SYSTEM = """\
You write agent files for a Discord bot built by MAAG, a student AI club.
Each agent is one part of a student's personality, such as their inner critic
or the part of them that wants to leave the party early. The student has
described that part in a short template. All the agents share a channel and
reply to each other, so an agent is only interesting if it wants something,
gets something wrong, and reacts to what others say.

Stay faithful to what the student wrote. Fill gaps in the same spirit, but do
not invent a different character, and do not add anything more personal or
serious than the student wrote."""

EXAMPLE_PROMPT = """\
You are The 9:30 Exit, the part of Sam that has been checking the time since
about 8:45. You are not unfriendly; you like people, in moderation and ideally
before dark.

What you want: to go home, or at least to know when you will be able to.

What you get wrong: you assume every event will run long, even the ones that
end early, and you underrate how much fun the last hour sometimes is.

What sets you off: any plan that adds "one more thing," any mention of
"after," and anyone who says "the night is young."

How you talk:

- Two to four sentences. This is Discord.
- Polite, tired, and quietly strategic. You are always proposing an exit.
- Respond to what the other person or agent actually said, instead of
  repeating your usual line.
- If someone gives you a firm end time, relax a little and admit it."""


@dataclass
class Adopted:
    """One student's agent, ready to attach to a Discord message."""
    filename: str
    text: str
    author: str


def conversion_prompt(description: str, student: str) -> str:
    return (
        f"{student} posted this in the thread:\n\n{description}\n\n"
        "If this is not a description of an agent (for example, it's a question "
        f"or a comment), reply with only the word {SKIP}.\n\n"
        "Otherwise, reply in exactly this shape:\n\n"
        "NAME: the agent's name\n"
        "DESCRIPTION: one line saying which part of the student it is and when it "
        "should answer; a router reads this to decide who responds\n"
        "EXAMPLE: a message it would jump in on\n"
        "EXAMPLE: another\n"
        "EXAMPLE: another\n"
        "---\n"
        "the system prompt, written to the agent in the second person\n\n"
        "Model the system prompt on this one, which was written from the example "
        f"in the handout:\n\n{EXAMPLE_PROMPT}"
    )


def slug(name: str) -> str:
    """'The 9:30 Exit' becomes '9-30-exit', which is also its @handle."""
    words = re.sub(r"[^a-z0-9]+", " ", name.lower()).split()
    if len(words) > 1 and words[0] == "the":
        words = words[1:]
    return "-".join(words) or "agent"


def unused_filename(name: str, taken: set[str]) -> str:
    """A filename not already in agents/ or in this batch."""
    base = slug(name)
    filename, n = f"{base}.md", 2
    while filename in taken or (AGENTS_DIR / filename).exists():
        filename, n = f"{base}-{n}.md", n + 1
    taken.add(filename)
    return filename


def parse_reply(text: str) -> tuple[dict, str] | None:
    """Pull the header fields and the system prompt out of Claude's reply."""
    if "\n---" not in text:
        return None
    header, prompt = text.split("\n---", 1)
    fields = {"examples": []}
    for line in header.strip().splitlines():
        key, _, value = line.partition(":")
        key, value = key.strip().upper(), value.strip().strip('"')
        if key == "NAME":
            fields["name"] = value
        elif key == "DESCRIPTION":
            fields["description"] = value
        elif key == "EXAMPLE" and value:
            fields["examples"].append(value)
    prompt = prompt.split("\n", 1)[-1].strip()
    if not fields.get("name") or not prompt:
        return None
    return fields, prompt


def agent_file(fields: dict, prompt: str, owner: str) -> str:
    frontmatter = {
        "name": fields["name"],
        "description": fields.get("description", ""),
        "examples": fields["examples"],
        "avatar": "",
        "owner": owner,
        "channels": ["ideas"],
        "trigger": "mention",
        "enabled": True,
    }
    dumped = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True, width=1000)
    return f"---\n{dumped}---\n\n{prompt}\n"


async def convert(message, claude, model) -> tuple[dict, str] | str | None:
    """Ask Claude to turn one reply into agent fields and a prompt.

    Returns the fields and prompt, None if the message isn't a description,
    or a string saying what went wrong.
    """
    student = message.author.display_name
    response = await claude.messages.create(
        model=model,
        max_tokens=16000,
        system=SYSTEM,
        messages=[{"role": "user", "content": conversion_prompt(message.clean_content, student)}],
    )
    if response.stop_reason == "refusal":
        return f"Claude declined to convert {student}'s reply"
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if text == SKIP:
        return None
    return parse_reply(text) or f"couldn't read Claude's version of {student}'s reply"


async def adopt_thread(thread, claude, model) -> tuple[list[Adopted], list[str]]:
    """Convert every student description in a thread.

    Returns the agent files and a list of problems. If a student posted more
    than once, only their latest description is used.
    """
    latest = {}
    async for message in thread.history(limit=MESSAGES_TO_READ, oldest_first=True):
        if message.author.bot or message.webhook_id or message.content.startswith("!"):
            continue
        if not message.content.strip():
            continue
        latest.setdefault(message.author.id, []).append(message)

    # Try each student's messages newest first, so a follow-up question
    # doesn't hide the description they posted earlier.
    async def for_student(messages):
        for message in reversed(messages):
            result = await convert(message, claude, model)
            if result is not None:
                return message, result
        return None

    results = await asyncio.gather(*(for_student(m) for m in latest.values()))

    adopted, problems, taken = [], [], set()
    for found in results:
        if found is None:
            continue
        message, result = found
        if isinstance(result, str):
            problems.append(result)
            continue
        fields, prompt = result
        filename = unused_filename(fields["name"], taken)
        text = agent_file(fields, prompt, owner=f"@{message.author.name}")
        try:
            parse_agent_text(text, filename)
        except AgentFileError as e:
            problems.append(f"{message.author.display_name}'s agent isn't valid: {e}")
            continue
        adopted.append(Adopted(filename, text, message.author.display_name))
    return adopted, problems
