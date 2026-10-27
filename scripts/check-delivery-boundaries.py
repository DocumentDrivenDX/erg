"""Keep portable delivery code free of collection, reporting and cloud SDKs."""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWED = {
    "delivery_protocol": set(),
    "delivery_journal": {"delivery_protocol"},
    "delivery_client": {"delivery_protocol", "delivery_journal"},
    "delivery_inventory": {"delivery_protocol"},
}
STDLIB = {
    "base64",
    "collections",
    "hashlib",
    "json",
    "math",
    "os",
    "pathlib",
    "random",
    "re",
    "selectors",
    "sqlite3",
    "subprocess",
    "sys",
    "tempfile",
    "time",
    "urllib",
    "uuid",
    "zlib",
}
errors = []
for name, allowed in ALLOWED.items():
    path = ROOT / "erg" / (name + ".py")
    if not path.exists():
        continue
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            imports = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            imports = [node.module] if node.module else [a.name for a in node.names]
            if node.level and any(v not in allowed for v in imports):
                errors.append("%s:%s: forbidden local import" % (path, node.lineno))
        else:
            continue
        for value in imports:
            if value.split(".")[0] not in STDLIB and value not in allowed:
                errors.append("%s:%s: forbidden dependency %s" % (path, node.lineno, value))
if errors:
    print("\n".join(errors), file=sys.stderr)
    sys.exit(1)
print("Delivery boundaries passed")
