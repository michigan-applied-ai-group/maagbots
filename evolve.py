"""Lets an agent rewrite itself, using 👍/👎 reactions as the selection pressure.

The bot never writes to agents/. It only ever *proposes* a new version of an
agent file. A human opens the pull request, and merging it is the moment of
selection — which also means every generation of an agent is a commit, so the
whole lineage is in git history.
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from loader import AGENTS_DIR, Agent, AgentFileError, parse_agent_text

log = logging.getLogger("maagbots.evolve")

THUMBS_UP = "👍"
THUMBS_DOWN = "👎"
MESSAGES_PER_PLACE = 50     # how far back to look in the channel and each thread
MAX_THREADS = 10            # how many of the channel's threads to read
SAMPLES_EACH_WAY = 3        # how many liked and disliked replies to learn from
DO_NOT_CHANGE = "## Do not change"

# Frontmatter an agent may never rewrite about itself.
FIXED_FIELDS = ("name", "owner", "channels", "trigger", "enabled")


@dataclass
class Sample:
    """One reply the agent gave, and how the club reacted to it."""
    reply: str      # what the agent said
    prompt: str     # the message it was answering
    score: int      # 👍 minus 👎


def reaction_score(message) -> int:
    score = 0
    for reaction in message.reactions:
        if str(reaction.emoji) == THUMBS_UP:
            score += reaction.count
        elif str(reaction.emoji) == THUMBS_DOWN:
            score -= reaction.count
    return score


async def collect_feedback(channel, agent: Agent) -> list[Sample]:
    """Find the agent's recent replies in this channel and its threads."""
    samples = []
    for place in [channel, *channel.threads[:MAX_THREADS]]:
        # History arrives newest first, so the message an agent was answering
        # is the *next* one we see.
        waiting = None
        async for message in place.history(limit=MESSAGES_PER_PLACE):
            if waiting is not None:
                samples.append(
                    Sample(
                        reply=waiting.clean_content,
                        prompt=message.clean_content,
                        score=reaction_score(waiting),
                    )
                )
                waiting = None
            if message.webhook_id and message.author.name == agent.name:
                waiting = message
    return samples


def best_and_worst(samples: list[Sample]) -> tuple[list[Sample], list[Sample]]:
    """Split the samples people actually reacted to into liked and disliked."""
    liked = sorted((s for s in samples if s.score > 0), key=lambda s: -s.score)
    disliked = sorted((s for s in samples if s.score < 0), key=lambda s: s.score)
    return liked[:SAMPLES_EACH_WAY], disliked[:SAMPLES_EACH_WAY]


def protected_block(system_prompt: str) -> str:
    """The part of a prompt marked '## Do not change', if there is one."""
    if DO_NOT_CHANGE not in system_prompt:
        return ""
    block = system_prompt.split(DO_NOT_CHANGE, 1)[1]
    return DO_NOT_CHANGE + re.split(r"\n## ", block)[0].rstrip()


def check_proposal(original: Agent, proposed_text: str) -> tuple[Agent | None, list[str]]:
    """Parse a proposed file and check it kept everything it had to keep."""
    try:
        proposed = parse_agent_text(proposed_text, original.source)
    except AgentFileError as e:
        return None, [f"the proposed file isn't a valid agent file: {e}"]

    problems = []
    for field in FIXED_FIELDS:
        was, now = getattr(original, field), getattr(proposed, field)
        if was != now:
            problems.append(f"'{field}' changed from {was!r} to {now!r}")

    protected = protected_block(original.system_prompt)
    if protected and protected not in proposed.system_prompt:
        problems.append(f"the '{DO_NOT_CHANGE}' block was altered or dropped")

    return (proposed if not problems else None), problems


def rewrite_prompt(agent: Agent, liked: list[Sample], disliked: list[Sample]) -> str:
    current = (AGENTS_DIR / agent.source).read_text(encoding="utf-8")

    def show(samples):
        return "\n\n".join(
            f"Someone said: {s.prompt}\n{agent.name} replied: {s.reply}\nScore: {s.score:+d}"
            for s in samples
        ) or "(none)"

    protected = protected_block(agent.system_prompt)
    return (
        f"Here is the current agent file, agents/{agent.source}:\n\n"
        f"```markdown\n{current}\n```\n\n"
        f"Replies the club reacted well to:\n\n{show(liked)}\n\n"
        f"Replies the club reacted badly to:\n\n{show(disliked)}\n\n"
        "Rewrite the file so this agent gives more replies like the first group "
        "and fewer like the second. Keep its character: this is the same agent "
        "getting better at its job, not a different one. Change only the "
        "'description', the 'examples', and the prompt body.\n\n"
        "Rules you must follow:\n"
        f"- Copy these frontmatter fields exactly as they are: {', '.join(FIXED_FIELDS)}.\n"
        + (
            f"- Copy the '{DO_NOT_CHANGE}' block word for word:\n\n{protected}\n\n"
            if protected
            else ""
        )
        + "- Keep the file in the same format: YAML frontmatter between '---' "
        "lines, then the prompt.\n\n"
        "Reply in exactly this shape:\n\n"
        "SUMMARY: two or three sentences on what you changed and why, citing the "
        "reactions.\n"
        "---BEGIN FILE---\n"
        "(the complete new file)\n"
        "---END FILE---"
    )


def strip_fence(text: str) -> str:
    """Drop a ```markdown ... ``` wrapper, which the model often adds."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def split_reply(text: str) -> tuple[str, str]:
    """Pull the summary and the proposed file out of the model's reply.

    Models are loose about wrapping: fences, stray chatter before the
    frontmatter, sometimes no markers at all. Take the file anyway.
    """
    before = text.split("---BEGIN FILE---")[0]
    # Without the marker, `before` is the whole reply, so stop at the file itself.
    summary = re.split(r"\n```|\n---\n", before)[0].removeprefix("SUMMARY:").strip()
    match = re.search(r"---BEGIN FILE---\n(.*?)\n---END FILE---", text, re.DOTALL)
    body = strip_fence(match.group(1) if match else text)
    if not body.startswith("---") and "---\n" in body:
        body = body[body.index("---\n"):]  # skip anything said before the frontmatter
    return summary, (body + "\n" if body else "")


async def propose_rewrite(agent, samples, claude, model) -> tuple[str, str, list[str]]:
    """Ask Claude for a new version of the agent file.

    Returns the summary, the proposed file, and any problems with it. When
    there are problems, the file is not safe to propose.
    """
    liked, disliked = best_and_worst(samples)
    if not liked and not disliked:
        return "", "", ["nobody has reacted to this agent's replies yet"]

    response = await claude.messages.create(
        model=model,
        max_tokens=16000,
        system=(
            "You improve the definition files of agents in a Discord bot built "
            "by a student AI club. You are careful and conservative: you make "
            "the smallest change that answers the feedback."
        ),
        messages=[{"role": "user", "content": rewrite_prompt(agent, liked, disliked)}],
    )
    if response.stop_reason == "refusal":
        return "", "", ["Claude declined to rewrite this agent"]

    text = "".join(b.text for b in response.content if b.type == "text")
    summary, proposed_text = split_reply(text)
    if not proposed_text:
        return summary, "", ["Claude's reply didn't contain a file"]

    _, problems = check_proposal(agent, proposed_text)
    if problems:
        log.warning(
            "Rejected a new %s (%s). The proposal started: %r",
            agent.name, "; ".join(problems), proposed_text[:120],
        )
    return summary, proposed_text, problems
