from __future__ import annotations

import json
import unittest

from pydantic import ValidationError

from stage1_story_agent.agent import Stage1StoryAgent
from stage1_story_agent.backends import FakeBackend
from stage1_story_agent.models import MuseCocoAttributeTargets
from stage1_story_agent.musecoco_encoder import (
    ATT_KEY,
    AUTOMATIC_NA_TOKENS,
    GENRES,
    INSTRUMENTS,
    NUM_LABELS,
    TOKENS_PER_SAMPLE,
    MuseCocoEncodingError,
    build_encoding_files,
    build_encoding_payloads,
    build_task_package_files,
    encode_targets,
    flatten_labels,
    labels_to_tokens,
    validate_combined_labels,
)
from stage1_story_agent.tests.fixtures import draft_data, make_request
from stage1_story_agent.utils import sha256_hex


def make_delivery():
    response = json.dumps(draft_data(), ensure_ascii=False)
    return Stage1StoryAgent(FakeBackend([response])).plan(make_request()).musecoco_delivery


class MuseCocoEncoderTests(unittest.TestCase):
    def test_closed_world_instrument_and_genre_encoding(self):
        targets = make_delivery().theme_families[0].musecoco_attribute_targets
        labels = encode_targets(targets)

        instruments = labels["I1s2"]
        self.assertEqual(instruments[INSTRUMENTS.index("piano")], [1, 0, 0])
        self.assertEqual(instruments[INSTRUMENTS.index("cello")], [1, 0, 0])
        self.assertEqual(instruments[INSTRUMENTS.index("violin")], [0, 1, 0])
        self.assertNotIn([0, 0, 1], instruments)

        genres = labels["S4"]
        self.assertEqual(genres[GENRES.index("classical")], [1, 0, 0])
        self.assertTrue(
            all(
                vector == ([1, 0, 0] if name == "classical" else [0, 1, 0])
                for name, vector in zip(GENRES, genres)
            )
        )

    def test_scalar_positions_and_artist_aliases(self):
        source = make_delivery().theme_families[0].musecoco_attribute_targets
        labels = encode_targets(source)
        self.assertEqual(labels["R1"], [0, 1, 0])
        self.assertEqual(labels["R3"], [1, 0, 0, 0])
        self.assertEqual(labels["S2s1"][2], 1)
        self.assertEqual(labels["TS1s1"], [1, 0, 0, 0, 0, 0, 0, 0])
        self.assertEqual(labels["K1"], [0, 1, 0])
        self.assertEqual(labels["P4"][2], 1)

        data = source.model_dump(mode="json")
        data["S2s1"] = "bach"
        bach = MuseCocoAttributeTargets.model_validate(data)
        self.assertEqual(encode_targets(bach)["S2s1"][5], 1)
        data["S2s1"] = "handel"
        handel = MuseCocoAttributeTargets.model_validate(data)
        self.assertEqual(encode_targets(handel)["S2s1"][8], 1)

    def test_flatten_uses_official_60_head_order_and_dimensions(self):
        labels = encode_targets(
            make_delivery().theme_families[0].musecoco_attribute_targets
        )
        flat = flatten_labels(labels)
        self.assertEqual(tuple(flat), ATT_KEY)
        self.assertEqual(len(flat), 60)
        for key, vector in flat.items():
            self.assertEqual(len(vector), NUM_LABELS[key], key)
            self.assertEqual(sum(vector), 1, key)

    def test_token_conversion_matches_musecoco_order(self):
        labels = encode_targets(
            make_delivery().theme_families[0].musecoco_attribute_targets
        )
        tokens = labels_to_tokens(labels)
        self.assertEqual(len(tokens), TOKENS_PER_SAMPLE)
        self.assertEqual(TOKENS_PER_SAMPLE, 63)
        self.assertEqual(tokens[0], "I1s2_0_0")
        self.assertEqual(tokens[6], "I1s2_6_1")
        self.assertEqual(tokens[28:30], ["I4_28", "C1_4"])
        self.assertEqual(tokens[30], "R1_1")
        self.assertEqual(tokens[33 + GENRES.index("classical")], "S4_11_0")
        self.assertEqual(tokens[60], "ST1_14")
        self.assertEqual(
            {token for token in tokens if token in AUTOMATIC_NA_TOKENS},
            set(AUTOMATIC_NA_TOKENS),
        )

    def test_validator_rejects_na_in_known_multilabel_field(self):
        labels = encode_targets(
            make_delivery().theme_families[0].musecoco_attribute_targets
        )
        labels["I1s2"][0] = [0, 0, 1]
        with self.assertRaises(MuseCocoEncodingError):
            validate_combined_labels(labels)

    def test_project_policy_rejects_stravinsky_and_synthesizer(self):
        source = make_delivery().theme_families[0].musecoco_attribute_targets
        data = source.model_dump(mode="json")
        data["S2s1"] = "stravinsky"
        with self.assertRaisesRegex(ValidationError, "forbids MuseCoco artist"):
            MuseCocoAttributeTargets.model_validate(data)

        data = source.model_dump(mode="json")
        data["I1s2"] = ["synthesizer"]
        with self.assertRaisesRegex(ValidationError, "forbids MuseCoco instruments"):
            MuseCocoAttributeTargets.model_validate(data)

    def test_batch_payloads_preserve_family_order_and_official_shape(self):
        delivery = make_delivery()
        payloads = build_encoding_payloads(delivery)
        predict = payloads["predict.json"]
        index = payloads["predict_index.json"]
        direct = payloads["direct_attribute_labels.json"]
        flat = payloads["predict_attributes.json"]

        self.assertEqual(len(predict), 3)
        self.assertEqual(
            [sample["theme_family_id"] for sample in index["samples"]],
            ["theme-A", "theme-B", "theme-C"],
        )
        self.assertEqual(len(direct["samples"][0]["attribute_tokens"]), 63)
        self.assertEqual(tuple(flat), ATT_KEY)
        self.assertTrue(all(len(vectors) == 3 for vectors in flat.values()))

    def test_serialized_manifest_hashes_every_generated_payload(self):
        files = build_encoding_files(make_delivery())
        self.assertTrue(
            {
                "predict.json", "predict_index.json", "direct_attribute_labels.json",
                "predict_attributes.json", "encoding_manifest.json",
            }.issubset(files)
        )
        manifest = json.loads(files["encoding_manifest.json"])
        self.assertEqual(manifest["sample_count"], 3)
        self.assertEqual(manifest["official_head_count"], 60)
        self.assertEqual(manifest["tokens_per_sample"], 63)
        self.assertEqual(manifest["probability_source"], "deterministic_one_hot")
        self.assertEqual(manifest["task_package_count"], 3)
        for name, expected_hash in manifest["files"].items():
            self.assertEqual(sha256_hex(files[name]), expected_hash)
        flat = json.loads(files["predict_attributes.json"])
        self.assertEqual(tuple(flat), ATT_KEY)

    def test_task_packages_are_split_to_one_sample_with_text_and_audit(self):
        files = build_task_package_files(make_delivery())
        roots = sorted({name.split("/")[1] for name in files})
        self.assertEqual(len(roots), 3)
        for root in roots:
            prefix = f"task_packages/{root}/"
            package = {
                name.removeprefix(prefix): json.loads(data)
                for name, data in files.items()
                if name.startswith(prefix)
            }
            self.assertEqual(
                set(package),
                {"predict_attributes.json", "predict.json", "predict_index.json", "audit.json"},
            )
            self.assertEqual(len(package["predict.json"]), 1)
            self.assertTrue(package["predict.json"][0]["text"])
            self.assertTrue(
                all(len(batch) == 1 for batch in package["predict_attributes.json"].values())
            )
            self.assertEqual(package["audit.json"]["probability_source"], "deterministic_one_hot")
            self.assertFalse(package["audit.json"]["uses_stage1_model"])


if __name__ == "__main__":
    unittest.main()
