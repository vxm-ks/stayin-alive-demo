from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stage1_story_agent.config import Stage1Config
from stage1_story_agent.errors import ConfigurationError


class ConfigTests(unittest.TestCase):
    def test_reads_key_and_settings_from_local_env_file(self):
        with tempfile.TemporaryDirectory() as root:
            env_file = Path(root) / ".env"
            env_file.write_text(
                "DEEPSEEK_API_KEY=file-secret\n"
                "DEEPSEEK_MODEL=deepseek-v4-pro\n"
                "STAGE1_THINKING=disabled\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                config = Stage1Config.from_env(env_file=env_file)
            self.assertEqual(config.api_key.get_secret_value(), "file-secret")
            self.assertEqual(config.model, "deepseek-v4-pro")
            self.assertEqual(config.thinking, "disabled")

    def test_process_environment_overrides_dotenv(self):
        with tempfile.TemporaryDirectory() as root:
            env_file = Path(root) / ".env"
            env_file.write_text("DEEPSEEK_API_KEY=file-secret\n", encoding="utf-8")
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "process-secret"}, clear=True):
                config = Stage1Config.from_env(env_file=env_file)
            self.assertEqual(config.api_key.get_secret_value(), "process-secret")

    def test_unknown_dotenv_keys_are_ignored(self):
        with tempfile.TemporaryDirectory() as root:
            env_file = Path(root) / ".env"
            env_file.write_text("UNRELATED_SECRET=ignore-me\nDEEPSEEK_API_KEY=accepted\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                config = Stage1Config.from_env(env_file=env_file)
            self.assertEqual(config.api_key.get_secret_value(), "accepted")

    def test_malformed_dotenv_reports_line_without_content(self):
        with tempfile.TemporaryDirectory() as root:
            env_file = Path(root) / ".env"
            env_file.write_text("DEEPSEEK_API_KEY\n", encoding="utf-8")
            with self.assertRaises(ConfigurationError) as caught:
                Stage1Config.from_env(env_file=env_file)
            self.assertEqual(caught.exception.code, "DOTENV_LINE_INVALID")
            self.assertNotIn("DEEPSEEK_API_KEY", caught.exception.public_message)


if __name__ == "__main__":
    unittest.main()
