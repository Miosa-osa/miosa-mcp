import importlib.util
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "sync_action_capabilities.py"
SPEC = importlib.util.spec_from_file_location("sync_action_capabilities", MODULE_PATH)
SYNC = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SYNC)


class ActionCapabilitySyncTest(unittest.TestCase):
    def valid_contract(self):
        name = "sandbox.exec"
        version = "1.0.0"
        return {
            "version": 1,
            "capabilities": [
                {
                    "name": name,
                    "version": version,
                    "fingerprint": SYNC.expected_fingerprint(name, version),
                    "surfaces": {"mcp": ["sandbox_exec"]},
                }
            ],
        }

    def test_accepts_identity_and_aliases_only(self):
        SYNC.validate(self.valid_contract())

    def test_rejects_duplicate_tool_aliases(self):
        contract = self.valid_contract()
        second = dict(contract["capabilities"][0])
        second["name"] = "sandbox.python"
        second["fingerprint"] = SYNC.expected_fingerprint(second["name"], second["version"])
        contract["capabilities"].append(second)

        with self.assertRaisesRegex(ValueError, "duplicate MCP tool alias"):
            SYNC.validate(contract)

    def test_rejects_policy_fields(self):
        contract = self.valid_contract()
        contract["capabilities"][0]["approval"] = "never"

        with self.assertRaisesRegex(ValueError, "contains policy"):
            SYNC.validate(contract)


if __name__ == "__main__":
    unittest.main()
