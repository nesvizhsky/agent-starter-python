"""One-shot script: generate avatar images for all Disputatio personas and upload to R2.

Run once during setup (or re-run to regenerate a specific persona):

    uv run python scripts/generate_personas.py              # all personas
    uv run python scripts/generate_personas.py socrates     # one persona by key

Images land at  disputatio/personas/{key}.png  in your R2 bucket.
Requires: FAL_KEY, R2_* env vars set in .env.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

import httpx

from agent.services import media, storage
from disputatio.personas import PERSONAS, Persona

# Image generation prompt template — style is consistent across all avatars.
_STYLE = (
    "Portrait illustration in a bold, flat graphic style with rich colours. "
    "Square format, clean background, expressive face, iconic and immediately "
    "recognisable as a character archetype. No text. No watermark."
)


def _p(subject: str) -> str:
    return f"{subject} {_STYLE}"


_PROMPTS: dict[str, str] = {
    "socrates": _p(
        "Ancient Greek philosopher Socrates, white beard, toga, thoughtful questioning gaze."
    ),
    "stoic": _p(
        "Roman emperor Marcus Aurelius, calm and composed expression, laurel wreath,"
        " armour, steady eyes that have seen much."
    ),
    "pragmatist": _p(
        "Sharp-eyed modern thinker in plain clothes, sleeves rolled up, direct no-nonsense"
        " expression, holding a single sheet of paper with notes."
    ),
    "empiricist": _p(
        "Precise analytical figure in wire-rimmed glasses, lab coat, holding a magnifying"
        " glass over a document, meticulous and calm."
    ),
    "irina": _p(
        "Woman in her 40s, Eastern European features, knowing and measured expression,"
        " dark turtleneck, slight weariness in sharp eyes."
    ),
    "viktor": _p(
        "Well-groomed man in a neat suit, carefully composed smile that doesn't quite"
        " reach his eyes, holding a clipboard, confident posture."
    ),
    "marcus": _p(
        "Older male historian, reading glasses pushed up on forehead, surrounded by stacked"
        " books and open maps, thoughtful and serious expression."
    ),
    "diplomat": _p(
        "Silver-haired diplomat in formal attire, measured expression, hands clasped,"
        " flags subtly visible in background, air of quiet authority."
    ),
    "archivist": _p(
        "Figure surrounded by towering shelves of documents and folders, wearing gloves,"
        " carefully examining a yellowed page, focused and patient."
    ),
}


async def generate_one(persona: Persona, *, dry_run: bool = False) -> None:
    key = f"disputatio/personas/{persona.key}.png"
    prompt = _PROMPTS[persona.key]

    print(f"  [{persona.key}] generating image...")
    if dry_run:
        print(f"  [{persona.key}] DRY RUN — would upload to {key}")
        return

    result = await media.text_to_image(prompt)
    if not result.files:
        print(f"  [{persona.key}] ERROR: no image returned")
        return

    image_url = result.files[0].url
    print(f"  [{persona.key}] downloading from fal...")

    async with httpx.AsyncClient() as client:
        resp = await client.get(image_url)
        resp.raise_for_status()
        image_bytes = resp.content

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp.write(image_bytes)
        tmp_path = Path(tmp.name)

    print(f"  [{persona.key}] uploading to R2 at {key}...")
    await storage.upload_file(tmp_path, key)
    tmp_path.unlink()  # noqa: ASYNC240 — one-shot script, sync unlink is fine

    public = storage.public_url(key)
    print(f"  [{persona.key}] done → {public}")


async def main() -> None:
    target_keys = sys.argv[1:]  # optional: generate only these keys

    personas = [p for p in PERSONAS if p.key in target_keys] if target_keys else PERSONAS

    if target_keys and not personas:
        print(f"Unknown persona key(s): {target_keys}")
        print(f"Valid keys: {[p.key for p in PERSONAS]}")
        sys.exit(1)

    print(f"Generating {len(personas)} persona avatar(s)...\n")
    for persona in personas:
        await generate_one(persona)

    print("\nAll done.")


if __name__ == "__main__":
    asyncio.run(main())
