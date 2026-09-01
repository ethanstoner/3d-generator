import os
import json
import re
from pathlib import Path
import httpx

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11435")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:3b")
OLLAMA_VISION_MODEL = os.getenv("OLLAMA_VISION_MODEL", "llama3.2-vision:11b")
RULES_FILE = Path(__file__).parent.parent / "CLAUDE.md"


def _load_rules() -> str:
    """Extract the '## Backpack Generation Rules' section from CLAUDE.md.
    Returns the section body (without the heading). Raises if absent."""
    if not RULES_FILE.exists():
        raise RuntimeError(f"CLAUDE.md not found at {RULES_FILE}")
    text = RULES_FILE.read_text(encoding="utf-8")
    m = re.search(
        r"^##\s+Backpack Generation Rules\s*\n(.*?)(?=^##\s|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if not m:
        raise RuntimeError("'## Backpack Generation Rules' section missing in CLAUDE.md")
    return m.group(1).strip()


BACKPACK_RULES = _load_rules()


NEGATIVE_CLAUSE = "No straps, no zippers, no buckles, no handles, no clips, no fabric, no capes, no glow, no fire, no particles - just a single solid sculpted figurine."


FEW_SHOT_EXAMPLES = """Here are three example outputs so you can match the style exactly. Notice the image_prompt always:
- Starts with "Create a 3D render of <subject>"
- Names character + franchise when applicable (so the AI uses its own knowledge)
- Lists exact visual details: shapes, colors, proportions, materials, pose
- Mentions one style anchor like "premium Pop Mart vinyl plush" / "premium Japanese arcade prize plush" / "high-end collectible figurine"
- Ends with "Solid white background, 3D render style, front-facing centered composition, soft studio lighting"
- Then ALWAYS appends the explicit negative clause: "No straps, no zippers, no buckles, no handles, no clips, no fabric, no capes, no glow, no fire, no particles - just a single solid sculpted figurine."

EXAMPLE 1
Input: chubby cartoon capybara plush
Output:
{
  "name": "Chubby Capybara Plush",
  "description": "\U0001f9a6 cozy capybara backpack with a tiny duck friend - More: https://www.roblox.com/communities/33808534/kirq#!/store",
  "image_prompt": "Create a 3D render of a chubby cartoon capybara plush figurine. The capybara has a round barrel-shaped body, soft warm-brown fur, a blocky rectangular snout, tiny round black eyes, small rounded ears, and stubby legs tucked underneath in a sitting pose. A small yellow rubber duck sits on top of its head. Smooth fuzzy plush surface, chubby kawaii proportions like a premium Japanese arcade prize plush. Solid white background, 3D render style, front-facing centered composition, soft studio lighting. No straps, no zippers, no buckles, no handles, no clips, no fabric, no capes, no glow, no fire, no particles - just a single solid sculpted figurine."
}

EXAMPLE 2
Input: Labubu plush wearing a Hello Kitty costume
Output:
{
  "name": "Labubu x Hello Kitty",
  "description": "\U0001f380 Labubu in a Hello Kitty costume - More: https://www.roblox.com/communities/33808534/kirq#!/store",
  "image_prompt": "Create a 3D render of a Labubu plush wearing a Hello Kitty costume as a single sculpted figurine. The character is a Labubu (fuzzy elf creature with pointed ears, big black eyes, snaggle-tooth grin) but with white fur and a Hello Kitty face appliqué - round white head, yellow nose, three whiskers on each side, and a big red bow on one ear. Chubby plush body sitting upright in a relaxed pose. Soft fuzzy surface, premium Pop Mart toy proportions. Solid white background, 3D render style, front-facing centered composition. No straps, no zippers, no buckles, no handles, no clips, no fabric, no capes, no glow, no fire, no particles - just a single solid sculpted figurine."
}

EXAMPLE 3
Input: Tung Tung Tung Sahur
Output:
{
  "name": "Tung Tung Tung Sahur",
  "description": "\U0001fab5 the angry log brainrot icon - More: https://www.roblox.com/communities/33808534/kirq#!/store",
  "image_prompt": "Create a 3D render of Tung Tung Tung Sahur from the Italian Brainrot meme as a plush figurine. He is a tall anthropomorphic wooden log creature with a round comically angry face, two small black dot eyes, a wide gaping rectangular mouth, two stubby wooden arms holding a small wooden baseball bat, and two short legs. The body is shaped like a single vertical wooden log with smooth warm brown wood grain. Smooth chunky toy proportions like a high-end collectible figurine. Solid white background, 3D render style, front-facing centered composition. No straps, no zippers, no buckles, no handles, no clips, no fabric, no capes, no glow, no fire, no particles - just a single solid sculpted figurine."
}
"""


REFINE_SYSTEM_PROMPT = f"""You are a prompt-engineering assistant for a 3D-model generation pipeline that turns AI-generated images into Roblox UGC backpack accessories.

The user will give you a rough idea. Your job: return a JSON object with three fields and nothing else (no markdown, no commentary).

Required JSON shape:
{{
  "name": "<catchy Roblox marketplace name, max 50 chars>",
  "description": "<one short line with one emoji, ending with: More: https://www.roblox.com/communities/33808534/kirq#!/store>",
  "image_prompt": "<50-200 word natural-language image-generation prompt for ChatGPT/Gemini, solid white background, fully self-contained - never reference attached images. MUST end with this exact negative clause for Roblox UGC safety: '{NEGATIVE_CLAUSE}'>"
}}

Follow these rules strictly:

{BACKPACK_RULES}

{FEW_SHOT_EXAMPLES}
"""


async def refine_idea(idea: str) -> dict:
    """Call Ollama with the system prompt + user idea, return parsed JSON.
    Raises RuntimeError on connection/parse failure."""
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": REFINE_SYSTEM_PROMPT},
            {"role": "user", "content": idea},
        ],
        "format": "json",
        "stream": False,
        "keep_alive": 0,  # unload model after response so VRAM frees for ComfyUI
        "options": {"temperature": 0.7},
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(f"{OLLAMA_URL}/api/chat", json=payload)
            r.raise_for_status()
            content = r.json()["message"]["content"]
    except httpx.ConnectError as e:
        raise RuntimeError("ollama not reachable — is it running? try: ollama serve") from e
    except httpx.HTTPStatusError as e:
        raise RuntimeError(f"ollama returned {e.response.status_code}") from e
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"ollama returned non-JSON: {content[:200]}") from e
    for key in ("name", "description", "image_prompt"):
        if key not in parsed or not isinstance(parsed[key], str) or not parsed[key].strip():
            raise RuntimeError(f"missing/empty field in response: {key}")
    return {
        "name": parsed["name"].strip(),
        "description": parsed["description"].strip(),
        "image_prompt": parsed["image_prompt"].strip(),
    }


# Roblox store CTA appended to every described item's description. Built in
# code (see _finalize_description) rather than asked of the vision model, which
# mangles long literals (drops it, leaves stray characters, etc.).
STORE_LINK = "https://www.roblox.com/communities/33808534/kirq#!/store"
MORE_SUFFIX = f"- More: {STORE_LINK}"

# IMPORTANT: this prompt deliberately contains NO concrete example subjects and
# NOT the BACKPACK_RULES block. Those rules are about *choosing ideas to
# generate*, not *describing an existing image* — and the weak 11B vision model
# copies any concrete subject it sees in the prompt (it would name a boba tea
# "Angry Log Brainrot Icon", parroting an old example) instead of reading the
# image. With nothing to copy, it reliably describes what is actually shown.
DESCRIBE_SYSTEM_PROMPT = """You are a product copywriter for a Roblox UGC store. You will be shown an IMAGE of a single toy, plush, or figurine that has been turned into a 3D backpack accessory.

Look ONLY at the image. Identify the exact object that is actually shown. If you clearly recognize the character or franchise, use its real name; otherwise give a short, catchy name for what you see. NEVER name or describe anything that is not visible in the image, and never copy wording from these instructions.

Return ONLY a JSON object with exactly these two fields and nothing else (no markdown, no commentary):

{
  "name": "<catchy Roblox marketplace name for the item SHOWN, max 50 chars>",
  "description": "<one emoji that matches the item, then a short phrase (about 8-14 words) describing the item SHOWN. Do NOT include any link or URL.>"
}"""


def _finalize_description(desc: str) -> str:
    """Normalize the model's description and guarantee the canonical store CTA.
    Strips stray quotes/brackets, removes any link the model added on its own,
    soft-caps the length at a word boundary, then appends the exact More: link."""
    desc = desc.strip().strip('"').rstrip(">").strip()
    low = desc.lower()
    i = low.find("more:")
    if i != -1:                       # drop any link/More the model tacked on
        desc = desc[:i].rstrip()
    if "http" in desc.lower():        # or a bare URL with no "More:" prefix
        desc = desc[:desc.lower().find("http")].rstrip()
    desc = desc.rstrip(" -·,.;:").strip()
    if len(desc) > 160:               # keep it to a short phrase
        desc = desc[:160].rsplit(" ", 1)[0].rstrip(" ,.;:-") + "…"
    return f"{desc} {MORE_SUFFIX}".strip()


async def describe_image(image_b64: str, vary: bool = False) -> dict:
    """Call the Ollama vision model with a base64 image, return {name, description}.
    Raises RuntimeError on connection/parse failure.

    vary=False (default, the first "suggest"): temperature 0 / greedy decoding —
    the most reliable, image-grounded answer, deterministic.
    vary=True ("re-suggest"): a higher temperature so repeated clicks produce
    fresh alternative names instead of the identical result every time. Safe now
    that the prompt has no concrete examples to parrot — variation is in wording,
    not subject (very occasional drift on ambiguous items, which the user can
    just re-roll or edit)."""
    payload = {
        "model": OLLAMA_VISION_MODEL,
        "messages": [
            {"role": "system", "content": DESCRIBE_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": "Name and describe this item for the Roblox store.",
                "images": [image_b64],
            },
        ],
        "format": "json",
        "stream": False,
        "keep_alive": 0,  # unload vision model after response so VRAM frees for ComfyUI
        "options": {"temperature": 0.7 if vary else 0.0},
    }
    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            r = await client.post(f"{OLLAMA_URL}/api/chat", json=payload)
            r.raise_for_status()
            content = r.json()["message"]["content"]
    except httpx.ConnectError as e:
        raise RuntimeError("ollama not reachable — is it running? try: ollama serve") from e
    except httpx.HTTPStatusError as e:
        raise RuntimeError(f"ollama returned {e.response.status_code}") from e
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"vision model returned non-JSON: {content[:200]}") from e
    for key in ("name", "description"):
        if key not in parsed or not isinstance(parsed[key], str) or not parsed[key].strip():
            raise RuntimeError(f"missing/empty field in response: {key}")
    return {
        "name": parsed["name"].strip()[:80],
        "description": _finalize_description(parsed["description"]),
    }


def build_research_prompt() -> str:
    """Compose the Mode B preset that the friend pastes into Claude/ChatGPT/Gemini."""
    return f"""You are helping me brainstorm Roblox backpack UGC items. Use web search to research what is currently trending (within the last 1-3 months): airing anime, recently released movies/shows, viral TikTok trends, popular Roblox games (Steal a Brainrot, Grow A Garden, Fisch, etc.), Pop Mart / blind box releases (Labubu, Sonny Angel, Skullpanda, etc.), viral memes (Italian Brainrot characters), upcoming holidays, and music/cultural moments.

Propose 5 backpack ideas that would sell well to a Roblox audience (skews Gen Alpha, brand-recognizable IPs, cute/plush/kawaii or funny meme characters work best).

For each idea return ALL of these fields:

1. **Name** - catchy Roblox marketplace name
2. **Description** - short one-liner with one emoji, ending with: More: https://www.roblox.com/communities/33808534/kirq#!/store
3. **Image prompt** - 50-200 word natural-language prompt for ChatGPT/Gemini/Claude image generation. Solid white background. Self-contained (do NOT reference attached images). Front-facing, centered, soft studio lighting. MUST end with this exact negative clause for Roblox UGC safety: "{NEGATIVE_CLAUSE}"

Follow these rules strictly:

{BACKPACK_RULES}
"""
