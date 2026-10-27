"""Real Azure SDK registration/binding contract; never mocks function metadata."""

import importlib.util
import inspect
import sys
import unittest
from pathlib import Path

try:
    import azure.functions as functions

    AVAILABLE = functions is not None
except ImportError:
    AVAILABLE = False


@unittest.skipUnless(AVAILABLE, "central-only Azure Functions SDK unavailable")
class ReceiverIndex(unittest.TestCase):
    def test_registered_http_trigger_names_match_handler_parameters(self):
        root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(root / "central"))
        try:
            spec = importlib.util.spec_from_file_location(
                "erg_receiver_index", root / "central/receiver/function_app.py"
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            functions = module.app.get_functions()
            self.assertEqual(len(functions), 2)
            routes = set()
            for function in functions:
                handler = function.get_user_function()
                parameters = inspect.signature(handler).parameters
                triggers = [
                    b.get_dict_repr()
                    for b in function.get_bindings()
                    if b.get_dict_repr().get("type") == "httpTrigger"
                ]
                self.assertEqual(len(triggers), 1)
                trigger = triggers[0]
                self.assertIn(trigger["name"], parameters)
                self.assertEqual(set(parameters), {trigger["name"]})
                routes.add(trigger["route"])
            self.assertEqual(routes, {"v1/batches", "v1/delivery/status"})
        finally:
            sys.path.pop(0)
