import json
import os
from pathlib import Path
from typing import Any

import pytest

# Set before any Lambda module is imported: member_sync reads RESULT_BUS_NAME
# and MEMBER_TABLE_NAME at import, and boto3 clients need a region and (dummy)
# credentials.
os.environ.setdefault("RESULT_BUS_NAME", "member-results")
os.environ.setdefault("MEMBER_TABLE_NAME", "members")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def relayed_event() -> dict[str, Any]:
    return json.loads((FIXTURES / "relayed_event.json").read_text(encoding="utf-8"))
