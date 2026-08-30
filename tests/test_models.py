import pytest
from pydantic import ValidationError

from easy_prd2.models import CopyRequest


def valid_request():
    return {
        "source_connection_id": "source",
        "target_connection_id": "target",
        "source_organization_id": 1,
        "target_organization_id": 2,
        "workspace_id": 3,
        "queue_ids": [4],
        "target_workspace_name": "Demo",
        "target_queue_names": {4: "Invoices"},
    }


def test_copy_request_requires_unique_queue_ids():
    value = valid_request()
    value["queue_ids"] = [4, 4]
    with pytest.raises(ValidationError):
        CopyRequest.model_validate(value)


def test_copy_request_requires_a_queue():
    value = valid_request()
    value["queue_ids"] = []
    with pytest.raises(ValidationError):
        CopyRequest.model_validate(value)

