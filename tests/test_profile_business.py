import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class CredentialTests(unittest.TestCase):
    def test_asr_reader_uses_runtime_key_without_legacy_model_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            config.write_text("{}")
            with patch.dict(
                os.environ,
                {
                    "SCREEN_STUDIO_EDITOR_CONFIG": str(config),
                    "DASHSCOPE_API_KEY": "TEST_DASH_SCOPE",
                },
                clear=True,
            ):
                import sys
                sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
                import bailian_transcribe as asr

                self.assertEqual(asr._load_dashscope_api_key(), "TEST_DASH_SCOPE")


if __name__ == "__main__":
    unittest.main()
