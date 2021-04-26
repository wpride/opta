import json
from typing import Any, Callable, List
from unittest.mock import MagicMock, Mock

from pytest_mock import MockFixture

from opta.layer import Layer


class MockedCmdJsonOut:
    def __init__(self, out: dict):
        self.stdout = json.dumps(out).encode("utf-8")


class MockedCmdOut:
    def __init__(self, out: str):
        self.stdout = out.encode("utf-8")


def get_call_args(mocked_obj: MagicMock) -> List[Any]:
    raw_call_args = mocked_obj.call_args_list
    return [arg[0][0] for arg in raw_call_args]


# Mock a function and provide it an expected return value.
#
# Get the mock's call arguments by passing in a mutable dictionary,
# where "args" and "kwargs" k/v pairs will be substituted in.
def mocked_function(
    return_value: Any = None, call_args_placeholder: dict = {}
) -> Callable:
    def dummy_function(*args: list, **kwargs: dict) -> Any:
        call_args_placeholder["args"] = args
        call_args_placeholder["kwargs"] = kwargs
        return return_value

    return dummy_function


def mocked_aws_layer(mocker: MockFixture) -> "Mock":
    mocked_layer = mocker.Mock(spec=Layer)
    mocked_layer.name = "dummy_layer"
    mocked_layer.cloud = "aws"
    return mocked_layer


def mocked_gcp_layer(mocker: MockFixture) -> "Mock":
    mocked_layer = mocker.Mock(spec=Layer)
    mocked_layer.name = "dummy_layer"
    mocked_layer.cloud = "google"
    return mocked_layer
