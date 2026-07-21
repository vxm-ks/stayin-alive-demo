"""Generate one auditable modern, danceable MuseCoco task package offline."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from stage1_story_agent.models import (
    GlobalProposal,
    LLMThemeFamily,
    MuseCocoDelivery,
    ThemeFamily,
)
from stage1_story_agent.musecoco import derive_targets, render_musecoco_text
from stage1_story_agent.musecoco_encoder import build_encoding_files
from stage1_story_agent.utils import json_bytes


def build_delivery() -> MuseCocoDelivery:
    global_proposal = GlobalProposal.model_validate(
        {
            "tempo_bpm": 128,
            "time_signature": "4/4",
            "global_tonality": {
                "tonic": "C",
                "mode": "major",
                "rationale": "Bright tonal center for a modern dance-oriented test.",
            },
        }
    )
    draft_family = LLMThemeFamily.model_validate(
        {
            "base_symbol": "A",
            "role": "modern_dance_theme",
            "seed_bars": 8,
            "intent": "A bright, energetic modern electronic dance theme with a clear pulse.",
            "musecoco_choices": {
                "I1s2": ["keyboard", "bass", "drum"],
                "R1": "danceable",
                "R3": "high",
                "S2s1": "prokofiev",
                "S4": ["electronic"],
                "P4": 3,
            },
        }
    )
    targets = derive_targets(
        draft_family, global_proposal, "Q1", generation_bars=12
    )
    rendered = render_musecoco_text(draft_family, global_proposal, targets)
    family = ThemeFamily(
        theme_family_id="theme-A",
        base_symbol="A",
        introduced_in_section_id="S1",
        role=draft_family.role,
        seed_bars=draft_family.seed_bars,
        intent=draft_family.intent,
        musecoco_attribute_targets=targets,
        musecoco_text=rendered.text,
    )
    return MuseCocoDelivery(
        story_id="modern-danceable-demo",
        tempo_bpm=global_proposal.tempo_bpm,
        time_signature=global_proposal.time_signature,
        global_tonality=global_proposal.global_tonality,
        generation_target_bars=12,
        output_motif_bars=8,
        theme_families=[family],
    )


def default_output_dir() -> Path:
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    return Path("stage1_story_agent") / "outputs" / f"{stamp}-modern-danceable-musecoco"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    output_dir = (args.output_dir or default_output_dir()).resolve()
    if output_dir.exists():
        parser.error(f"output directory already exists: {output_dir}")

    delivery = build_delivery()
    files = {
        "musecoco_plan.json": json_bytes(delivery),
        **build_encoding_files(delivery),
    }
    for relative_name, data in files.items():
        destination = output_dir / relative_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    print(
        json.dumps(
            {
                "output_dir": str(output_dir),
                "task_count": 1,
                "musecoco_text": delivery.theme_families[0].musecoco_text,
                "attribute_targets": delivery.theme_families[0].musecoco_attribute_targets.model_dump(),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
