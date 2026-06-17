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
    "socrates": _p("Ancient Greek philosopher Socrates, white beard, toga, thoughtful gaze."),
    "monk": _p(
        "Medieval monk Brother Anselm, brown habit, quill in hand, candlelight,"
        " illuminated manuscript."
    ),
    "terminator": _p(
        "T-800 Terminator robot head with glowing red eye, metallic endoskeleton, dark background."
    ),
    "shakespeare": _p(
        "William Shakespeare, Elizabethan ruff collar, quill, dramatic theatrical expression."
    ),
    "sherlock": _p(
        "Sherlock Holmes, deerstalker hat, magnifying glass, sharp eyes, Victorian London."
    ),
    "senator": _p("Roman Senator in toga, laurel wreath, stern expression, marble columns."),
    "gonzo": _p(
        "Gonzo journalist with aviator sunglasses, cigarette holder, press badge, chaotic energy."
    ),
    "spin_doctor": _p(
        "Slick corporate PR consultant in sharp suit, megaphone, glossy smile, city skyline."
    ),
    "confucius": _p(
        "Ancient Chinese philosopher Confucius, flowing robes, long beard, serene expression,"
        " bamboo."
    ),
    "pirate": _p("Pirate captain with red beard, tricorn hat, telescope, stormy seas behind."),
    "explorer": _p(
        "Victorian gentleman explorer, pith helmet, monocle, safari jacket, map in hand."
    ),
    "conspiracy": _p(
        "Mysterious figure in dark hoodie surrounded by red string connecting newspaper"
        " clippings and photos."
    ),
    "commentator": _p(
        "Energetic sports commentator in stadium, headset microphone, pointing dramatically"
        " at camera."
    ),
    "existentialist": _p(
        "Brooding French philosopher in turtleneck, cigarette, Parisian café, existential stare."
    ),
    "child": _p(
        "Curious 5-year-old with big wondering eyes, raised hand as if asking a question, playful."
    ),
    "alien": _p(
        "Friendly alien anthropologist with large eyes, taking notes on a clipboard,"
        " observing humans."
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
