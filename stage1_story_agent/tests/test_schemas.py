from __future__ import annotations

import json
import unittest
from pathlib import Path

from stage1_story_agent.export_schemas import SCHEMAS


class SchemaSnapshotTests(unittest.TestCase):
    def test_checked_in_schemas_match_pydantic(self):
        directory = Path(__file__).parents[1] / "schemas"
        for filename, model in SCHEMAS.items():
            with self.subTest(filename=filename):
                checked_in = json.loads((directory / filename).read_text(encoding="utf-8"))
                self.assertEqual(checked_in, model.model_json_schema(by_alias=True))


if __name__ == "__main__":
    unittest.main()
