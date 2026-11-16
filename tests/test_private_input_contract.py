import unittest

from cardputer_mcp.private_input_contract import InputFile, composition_identity


class PrivateInputContractTests(unittest.TestCase):
    def args(self):
        return dict(
            project_id="fixture", principal="owner", input_principal="owner",
            input_project_id="fixture", source_receipt="a" * 64, input_receipt="b" * 64,
            source_commit="c" * 40, allowed_source_commit="c" * 40,
            registration_digest="d" * 64, recipe_digest="e" * 64,
            destination="main/private_input",
            source_files=(InputFile("main/main.c", 1, "f" * 64),),
            private_files=(InputFile("data.c", 2, "1" * 64),),
        )

    def test_deterministic_and_bound(self):
        args = self.args()
        expected = composition_identity(**args)
        self.assertEqual(expected, composition_identity(**args))
        for name in ("source_receipt", "input_receipt", "registration_digest", "recipe_digest"):
            with self.subTest(name=name):
                self.assertNotEqual(expected, composition_identity(**(args | {name: "9" * 64})))

    def test_owner_project_and_commit(self):
        for key in ("input_principal", "input_project_id", "allowed_source_commit"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                composition_identity(**(self.args() | {key: "9" * 40}))

    def test_paths_and_collision(self):
        for path in ("../x", "/x", "a\\b", "a:b", "AUX.txt", "a.", "", "a//b"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                composition_identity(**(self.args() | {"destination": path}))
        for path in ("main/private_input/data.c", "MAIN/PRIVATE_INPUT/DATA.C", "main"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                composition_identity(**(self.args() | {
                    "source_files": (InputFile(path, 1, "f" * 64),)}))

    def test_bad_size_digest_and_duplicate(self):
        for files in (
            (InputFile("x", True, "1" * 64),),
            (InputFile("x", -1, "1" * 64),),
            (InputFile("x", 257 * 1024 * 1024, "1" * 64),),
            (InputFile("x", 1, "wrong"),),
            (InputFile("x", 1, "1" * 64), InputFile("X", 1, "1" * 64)),
            (InputFile("x", 1, "1" * 64), InputFile("x/y", 1, "1" * 64)),
        ):
            with self.subTest(files=files), self.assertRaises(ValueError):
                composition_identity(**(self.args() | {"private_files": files}))

    def test_order_independent_but_content_bound(self):
        args = self.args() | {"private_files": (
            InputFile("b", 1, "1" * 64), InputFile("a", 1, "2" * 64))}
        expected = composition_identity(**args)
        self.assertEqual(expected, composition_identity(**(args | {
            "private_files": tuple(reversed(args["private_files"]))})))
        self.assertNotEqual(expected, composition_identity(**self.args()))
