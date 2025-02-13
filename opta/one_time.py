import os
import sys
from pathlib import Path

from opta.constants import CI, one_time_run


def one_time() -> None:
    if os.environ.get(CI) is not None:
        return

    if Path(one_time_run).is_file():
        return

    try:
        open(one_time_run, "w").close()
    except:  # noqa: E722
        sys.exit(1)

    print(
        "\nHi there, thanks for Using Opta.\nWe hope you have a great experience using Opta."
        "\nPlease reach out to us on slack.runx.dev for any queries."
    )
