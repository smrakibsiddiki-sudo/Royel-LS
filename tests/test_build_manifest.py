import unittest
from pathlib import Path

from royells_build_manifest import (
    generate_manifest,
    runtime_profile_from_environment,
    validate_manifest,
)


ROOT = Path(__file__).resolve().parents[1]


def manifest_environment(profile="generic"):
    return {
        "ROYELLS_RUNTIME_PROFILE": profile,
        "ROYELLS_ORACLE_PROFILE_LOCK": "1",
        "ROYELLS_API_ID": "12345",
        "ROYELLS_API_HASH": "manifest-test",
        "ROYELLS_BOT_TOKEN": "12345:manifest-test",
        "ROYELLS_USER_SESSION_STRING": "manifest-test",
        "ROYELLS_OWNER_ID": "12345",
        "ROYELLS_TARGET_CHAT_ID": "-10012345",
        "USE_SQLITE": "1",
    }


class BuildManifestTests(unittest.TestCase):
    def test_oracle_profile_lock_is_canonical_and_a1_is_allowed(self):
        self.assertEqual(
            runtime_profile_from_environment(manifest_environment("generic")),
            "oracle-e2-micro",
        )
        self.assertEqual(
            runtime_profile_from_environment(manifest_environment("a1")),
            "oracle-a1",
        )

    def test_manifest_generation_and_validation_execute_on_final_tree(self):
        environment = manifest_environment()
        manifest = generate_manifest(ROOT, environ=environment)
        self.assertEqual(manifest["deployment_profile"], "oracle-e2-micro")
        self.assertEqual(manifest["container_type"], "docker")
        result = validate_manifest(manifest, root=ROOT, environ=environment)
        self.assertFalse(result.fatal, result.fatal)


if __name__ == "__main__":
    unittest.main()
