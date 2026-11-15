"""--support-set: per-object metadata of objects with their own tree supports.

No Bambu binary needed (stock profile + stubbed profile loading).
Run: PYTHONPATH=skills/brickprint <blender python> -m unittest discover -s skills/brickprint/tests
"""

import unittest
from unittest import mock

from brickprint import settings, threemf

BASE_XML = ('<metadata key="enable_support" value="1"/>'
            '<metadata key="support_type" value="tree(auto)"/>'
            '<metadata key="bridge_no_support" value="1"/>')


def _cfg(**kw):
    return {"_composer": None, **kw}


class SupportMetadata(unittest.TestCase):
    def test_no_extras_is_the_historic_string(self):
        cfg = _cfg()
        self.assertEqual(threemf._support_metadata({"support": "plate"}, cfg), BASE_XML)
        self.assertEqual(threemf._support_metadata({"support": "plate"}), BASE_XML)
        self.assertEqual(threemf._support_metadata({"support": ""}, cfg), "")
        self.assertEqual(threemf.support_metadata(cfg), threemf.AUTO_SUPPORT_META)
        self.assertTrue(threemf.composer(cfg).legacy)   # empty extras keep the stock path

    def test_extras_appended_and_non_legacy(self):
        cfg = _cfg(support_set=["support_top_z_distance=0.64"])
        self.assertEqual(threemf._support_metadata({"support": "plate"}, cfg),
                         BASE_XML + '<metadata key="support_top_z_distance" value="0.64"/>')
        self.assertEqual(threemf.support_metadata(cfg)["support_top_z_distance"], "0.64")
        comp = threemf.composer(cfg)
        self.assertFalse(comp.legacy)
        self.assertEqual(comp.summary()["support_set"], {"support_top_z_distance": "0.64"})

    def test_base_key_override_rejected(self):
        for kv in ("enable_support=0", "support_type=normal(auto)", "bridge_no_support=0"):
            with self.assertRaises(settings.ProfileError):
                settings.parse_support_set(None, [kv])
        with self.assertRaises(settings.ProfileError):
            settings.parse_support_set({"support_type": "tree(auto)"}, None)
        with self.assertRaises(settings.ProfileError):
            settings.parse_support_set(None, ["no_equals_sign"])

    def test_cli_over_profile_per_key(self):
        prof = {"support_top_z_distance": 0.56, "support_interface_top_layers": 3}
        got = settings.parse_support_set(prof, ["support_top_z_distance=0.64"])
        self.assertEqual(got, {"support_top_z_distance": "0.64", "support_interface_top_layers": "3"})
        real = settings.load_profile

        def fake(kind, name):
            p = real(kind, name)
            if kind == "process":
                p = {**p, "supports": {"object_settings": prof}}
            return p
        with mock.patch.object(settings, "load_profile", fake):
            comp = settings.Composer(_cfg(support_set=["support_top_z_distance=0.64"]))
        self.assertEqual(comp.support_set, got)


if __name__ == "__main__":
    unittest.main()
