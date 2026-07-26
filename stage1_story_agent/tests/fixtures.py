from __future__ import annotations

from stage1_story_agent.models import LLMContentPlanDraft, StoryPlanRequest


def request_data() -> dict:
    return {
        "schema_version": "0.2-draft",
        "story_id": "story-001",
        "story_text": "平静开场后冲突出现，旧日信念以新的方式回归，最终作出决定。",
        "language": "zh-CN",
        "constraints": {
            "total_bars": 64,
            "target_form_sections": 4,
            "max_theme_families": 4,
            "max_variants_per_family": 2,
            "allowed_time_signatures": ["4/4"],
            "tempo_bpm_min": 70,
            "tempo_bpm_max": 130,
            "allowed_modes": ["minor"],
            "allowed_tonics": ["C", "D", "E", "F", "G", "A", "B"],
        },
    }


def draft_data() -> dict:
    choices = [
        (["piano", "cello"], "low", "chopin", ["classical"]),
        (["strings", "drum"], "high", "prokofiev", ["symphony"]),
        (["piano", "strings"], "medium", "beethoven", ["classical"]),
    ]
    return {
        "global_proposal": {
            "tempo_bpm": 96,
            "time_signature": "4/4",
            "global_tonality": {
                "tonic": "C",
                "mode": "minor",
                "rationale": "统一全曲调性，通过密度变化塑造转折。",
            },
        },
        "story_analysis": {
            "summary": "故事经历平静、冲突、回望和最终抉择。",
            "emotional_arc": [
                {"position": 0, "emotion": "calm", "valence": 0.2, "tension": 0.2},
                {"position": 0.5, "emotion": "anxious", "valence": -0.7, "tension": 0.8},
                {"position": 1, "emotion": "resolved", "valence": 0.5, "tension": 0.3},
            ],
            "narrative_segments": [
                {"segment_id": "N1", "summary": "平静开场", "narrative_role": "opening", "emotion": "calm", "valence": 0.2, "tension": 0.2},
                {"segment_id": "N2", "summary": "冲突出现", "narrative_role": "build", "emotion": "tense", "valence": -0.7, "tension": 0.7},
                {"segment_id": "N3", "summary": "旧主题回归", "narrative_role": "turn", "emotion": "melancholic", "valence": -0.6, "tension": 0.8},
                {"segment_id": "N4", "summary": "作出抉择", "narrative_role": "resolution", "emotion": "resolved", "valence": 0.5, "tension": 0.3},
            ],
        },
        "form_sections": [
            {"section_id": "S1", "base_symbol": "A", "variant_index": 0, "relation": "introduce", "source_section_id": None, "bar_count": 16, "tempo_bpm": 88, "narrative_segment_ids": ["N1"], "musical_intent": "建立克制的主主题"},
            {"section_id": "S2", "base_symbol": "B", "variant_index": 0, "relation": "introduce", "source_section_id": None, "bar_count": 16, "tempo_bpm": 104, "narrative_segment_ids": ["N2"], "musical_intent": "形成对比并提高张力"},
            {"section_id": "S3", "base_symbol": "A", "variant_index": 1, "relation": "variation", "source_section_id": "S1", "bar_count": 16, "tempo_bpm": 100, "narrative_segment_ids": ["N3"], "musical_intent": "保留 A 身份并提高密度"},
            {"section_id": "S4", "base_symbol": "C", "variant_index": 0, "relation": "introduce", "source_section_id": None, "bar_count": 16, "tempo_bpm": 92, "narrative_segment_ids": ["N4"], "musical_intent": "引入最终抉择的新主题"},
        ],
        "theme_families": [
            {
                "base_symbol": symbol,
                "role": role,
                "seed_bars": 8,
                "intent": f"{symbol} 家族主题意图",
                "musecoco_choices": {
                    "I1s2": values[0], "R1": "not_danceable", "R3": values[1],
                    "S2s1": values[2], "S4": values[3], "P4": 2,
                },
            }
            for symbol, role, values in zip(
                ["A", "B", "C"], ["main_theme", "conflict_theme", "resolution_theme"], choices
            )
        ],
    }


def make_request() -> StoryPlanRequest:
    return StoryPlanRequest.model_validate(request_data())


def make_draft() -> LLMContentPlanDraft:
    return LLMContentPlanDraft.model_validate(draft_data())


def test_mode_draft_data() -> dict:
    data = draft_data()
    data["story_analysis"]["narrative_segments"] = data["story_analysis"]["narrative_segments"][:3]
    data["form_sections"] = data["form_sections"][:3]
    data["form_sections"][0]["bar_count"] = 16
    data["form_sections"][1]["bar_count"] = 16
    data["form_sections"][2].update(
        {
            "variant_index": 0,
            "relation": "reprise",
            "source_section_id": "S1",
            "bar_count": 16,
            "musical_intent": "再现8小节A主题并续写8小节",
        }
    )
    data["theme_families"] = data["theme_families"][:2]
    for section in data["form_sections"]:
        section["tempo_bpm"] = 96
    return data


def make_test_mode_request() -> StoryPlanRequest:
    return StoryPlanRequest(
        story_id="test-mode-story",
        story_text="平静开始，冲突展开，最终回到最初主题。",
        language="zh-CN",
        test_mode=True,
    )
