import tempfile
import unittest
from pathlib import Path

from project import CircuitStore, execute_route, parse_input, plan_route


class FakeClient:
    def __init__(self):
        self.calls = []

    def complete(self, system, user, model, max_tokens):
        self.calls.append(model)
        if model == "strong-model":
            raise RuntimeError("example provider failure")
        return "local test response"


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.circuit = CircuitStore(Path(self.temp.name) / "circuits.sqlite3")
        self.profiles = {
            "cheap": {"model": "cheap-model", "input_price": 0.5, "output_price": 2},
            "strong": {"model": "strong-model", "input_price": 3, "output_price": 12},
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_budget_excludes_expensive_model(self):
        routes, *_ = plan_route("比较两种方案并给出多步推导", self.profiles, 0.002, self.circuit)
        self.assertEqual([route["role"] for route in routes], ["cheap"])

    def test_failure_fallback_uses_second_model(self):
        routes, *_ = plan_route("比较两种方案并给出多步推导", self.profiles, None, self.circuit)
        client = FakeClient()
        answer, profile, trace = execute_route("question", routes, client, self.circuit)
        self.assertEqual(client.calls, ["strong-model", "cheap-model"])
        self.assertEqual(profile["role"], "cheap")
        self.assertTrue(answer)

    def test_fallback_respects_remaining_estimated_budget(self):
        routes = [
            {"role": "strong", "model": "strong-model", "estimated_cost": 0.004},
            {"role": "cheap", "model": "cheap-model", "estimated_cost": 0.002},
        ]
        client = FakeClient()
        with self.assertRaises(ValueError):
            execute_route("question", routes, client, self.circuit, budget=0.005)
        self.assertEqual(client.calls, ["strong-model"])

    def test_circuit_opens_and_recovers(self):
        self.circuit.record("m", False, now=100)
        self.circuit.record("m", False, now=101)
        self.assertFalse(self.circuit.available("m", now=102))
        self.assertTrue(self.circuit.available("m", now=162))
        self.circuit.record("m", True, now=162)
        self.assertTrue(self.circuit.available("m", now=163))

    def test_invalid_budget(self):
        for budget in ("0", "-1", "nan", "garbage"):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                parse_input(f"预算USD={budget}\nquestion")


if __name__ == "__main__":
    unittest.main()
