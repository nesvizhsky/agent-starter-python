"""One-off repair: split any jammed-together source/excluded_source entries
caused by a bug in the inline "add source to an existing topic" flow
(on_text's awaiting_source_id/awaiting_block_id handlers) — those handlers
treated an entire multi-source message as one domain string instead of
splitting it, so pasting several sources at once produced one topic.sources
entry containing all of them concatenated with embedded separators (commas/
semicolons/newlines). Fixed in code; this repairs data already saved with
the bug before the fix existed.

A topic is only touched if at least one of its source/excluded_source
entries actually contains a separator character — most topics are
untouched. Re-splits with the same _split_sources() logic the input parser
now uses, dedupes, and writes the corrected list back.

Defaults to a dry run — prints affected topics only, writes nothing. Pass
--apply to actually update the database. Always backs up the old values to
a timestamped JSON file before writing, so this is reversible.

    uv run python scripts/repair_sources.py              # dry run
    uv run python scripts/repair_sources.py --apply       # actually update
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from elephant import store
from elephant.bot import _split_sources

_BACKUP_DIR = Path(__file__).parent / "backfill_backups"


def _repair(values: list[str]) -> list[str]:
    """Re-split any jammed entries, dedupe, preserve order."""
    fixed: list[str] = []
    for v in values:
        fixed.extend(_split_sources(v))
    seen: set[str] = set()
    deduped: list[str] = []
    for v in fixed:
        if v not in seen:
            seen.add(v)
            deduped.append(v)
    return deduped


def _is_jammed(values: list[str]) -> bool:
    return any(_split_sources(v) != [v] for v in values)


async def main(apply: bool) -> None:
    topics = await store.get_all_active_topics()
    affected = [t for t in topics if _is_jammed(t.sources) or _is_jammed(t.excluded_sources)]
    print(f"{len(topics)} active topic(s) checked, {len(affected)} affected.\n")

    backups: list[dict[str, object]] = []

    for topic in affected:
        new_sources = _repair(topic.sources)
        new_excluded = _repair(topic.excluded_sources)

        print(f"=== {topic.name} ===")
        if new_sources != topic.sources:
            print(f"  sources:          {topic.sources!r}")
            print(f"               ->   {new_sources!r}")
        if new_excluded != topic.excluded_sources:
            print(f"  excluded_sources: {topic.excluded_sources!r}")
            print(f"               ->   {new_excluded!r}")
        print()

        backups.append(
            {
                "topic_id": str(topic.id),
                "name": topic.name,
                "old_sources": topic.sources,
                "old_excluded_sources": topic.excluded_sources,
            }
        )

        if apply:
            await store.update_topic(topic.id, sources=new_sources, excluded_sources=new_excluded)

    if not affected:
        print("Nothing to repair.")
        return

    if apply:
        _BACKUP_DIR.mkdir(exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        backup_path = _BACKUP_DIR / f"sources_repair_backup_{stamp}.json"
        backup_path.write_text(json.dumps(backups, indent=2, ensure_ascii=False))
        print(f"Applied. Old values backed up to {backup_path}")
    else:
        print("Dry run only — no changes written. Pass --apply to update the database.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Actually write changes to the DB")
    args = parser.parse_args()
    asyncio.run(main(args.apply))
