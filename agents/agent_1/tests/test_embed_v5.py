from __future__ import annotations

import math
import os
import unittest

os.environ.setdefault("AGENT_1_DB_DSN", "postgresql://placeholder")

from agent_1.embed_v5 import (  # noqa: E402
    EMBED_DIMS,
    cap_text,
    parse_embeddings_response,
    truncate_normalize,
    vector_literal,
)


class TruncateNormalizeTests(unittest.TestCase):
    def test_truncates_to_target_dims_and_unit_norm(self) -> None:
        vec = [float(i) for i in range(1536)]  # native-size input
        out = truncate_normalize(vec, dims=EMBED_DIMS)
        self.assertEqual(len(out), EMBED_DIMS)
        self.assertAlmostEqual(math.sqrt(sum(v * v for v in out)), 1.0, places=6)
        # direction of the first 1024 comps is preserved (monotone here)
        self.assertLess(out[0], out[1])

    def test_already_1024_is_renormalized(self) -> None:
        vec = [3.0] + [0.0] * (EMBED_DIMS - 1)
        out = truncate_normalize(vec)
        self.assertEqual(len(out), EMBED_DIMS)
        self.assertAlmostEqual(out[0], 1.0, places=6)

    def test_zero_vector_is_left_as_is(self) -> None:
        out = truncate_normalize([0.0] * 2000)
        self.assertEqual(len(out), EMBED_DIMS)
        self.assertTrue(all(v == 0.0 for v in out))


class VectorLiteralTests(unittest.TestCase):
    def test_pgvector_bracket_format(self) -> None:
        self.assertEqual(vector_literal([0.5, -1.0, 2.0]), "[0.5,-1.0,2.0]")


class CapTextTests(unittest.TestCase):
    def test_caps_length(self) -> None:
        self.assertEqual(cap_text("абвгд", 3), "абв")
        self.assertEqual(cap_text("", 5), "")


class ParseResponseTests(unittest.TestCase):
    def test_orders_by_index(self) -> None:
        payload = {"data": [
            {"index": 1, "embedding": [2.0]},
            {"index": 0, "embedding": [1.0]},
        ]}
        self.assertEqual(parse_embeddings_response(payload, 2), [[1.0], [2.0]])

    def test_falls_back_to_response_order_without_index(self) -> None:
        payload = {"data": [{"embedding": [1.0]}, {"embedding": [2.0]}]}
        self.assertEqual(parse_embeddings_response(payload, 2), [[1.0], [2.0]])

    def test_wrong_count_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_embeddings_response({"data": [{"index": 0, "embedding": [1.0]}]}, 2)

    def test_empty_vector_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_embeddings_response({"data": [{"index": 0, "embedding": []}]}, 1)


if __name__ == "__main__":
    unittest.main()