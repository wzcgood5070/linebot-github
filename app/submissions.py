import re
from collections.abc import Iterable
from pathlib import Path


SUBMISSION_TAIL = re.compile(r".+_[0-9]{8}\.[^.]+\Z")


def changed_submissions(current: dict[str, str], previous: dict[str, str | None]) -> set[str]:
    return {name for name, version in current.items() if previous.get(name) != version}


def submissions_by_member(files: Iterable[Path], members: Iterable[str]) -> dict[str, list[Path]]:
    assigned: dict[str, list[Path]] = {name: [] for name in members}
    for path in files:
        names, separator, tail = path.name.partition("_")
        if not separator or not SUBMISSION_TAIL.fullmatch(tail):
            continue
        for name in dict.fromkeys(names.split(".")):
            if name in assigned:
                assigned[name].append(path)
    return assigned
