"""HTTP workbench tests. The server runs entirely on localhost and stdlib."""

from __future__ import annotations

import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from kentsugi.web import make_server, process_document


class WebApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = make_server("127.0.0.1", 0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def get_json(self, path: str) -> dict:
        with urlopen(self.base + path, timeout=3) as response:
            return json.loads(response.read())

    def post_json(self, path: str, payload: object) -> tuple[int, dict]:
        request = Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, json.loads(error.read())

    def test_index_and_catalog_are_available(self) -> None:
        with urlopen(self.base + "/", timeout=3) as response:
            html = response.read().decode("utf-8")
        self.assertIn("KENTSUGI", html)
        catalog = self.get_json("/api/catalog")
        self.assertGreaterEqual(len(catalog["assets"]), 6)
        self.assertIn("soda_can", {asset["id"] for asset in catalog["assets"]})

    def test_example_can_be_baked_to_svg(self) -> None:
        example = self.get_json("/api/example")["document"]
        status, result = self.post_json("/api/preview", {"document": example})
        self.assertEqual(200, status)
        self.assertTrue(result["valid"])
        self.assertTrue(result["svg"].startswith("<svg"))
        self.assertEqual("city", result["plan"]["environment"]["preset"])

    def test_invalid_document_returns_structured_issues(self) -> None:
        status, result = self.post_json("/api/validate", {"document": {"objects": []}})
        self.assertEqual(422, status)
        self.assertFalse(result["valid"])
        self.assertIn("missing", {issue["code"] for issue in result["issues"]})

    def test_process_document_does_not_mutate_input(self) -> None:
        document = {"objects": []}
        before = json.dumps(document, sort_keys=True)
        result, status = process_document(document, self.server.RequestHandlerClass.catalog)
        self.assertEqual(422, status)
        self.assertEqual(before, json.dumps(document, sort_keys=True))
        self.assertFalse(result["valid"])


if __name__ == "__main__":
    unittest.main()
