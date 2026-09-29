"""Version ordering, semver-style: 1.10.0 > 1.9.0, 2.0.0-beta.2 < 2.0.0, v1.2 == 1.2.0.

Non-numeric parts sort after numeric ones, so any string gets a stable order.
"""


def _part(part: str) -> tuple:
    return (0, int(part), "") if part.isdigit() else (1, 0, part)


def version_key(version: str) -> tuple:
    core, _, pre = version.strip().lstrip("vV").partition("+")[0].partition("-")
    numbers = [_part(p) for p in core.split(".")]
    numbers += [(0, 0, "")] * (3 - len(numbers))  # 1.2 == 1.2.0
    # A pre-release sorts before the release: 2.0.0-beta < 2.0.0.
    pre_key = (0, tuple(_part(p) for p in pre.split("."))) if pre else (1, ())
    return (tuple(numbers), pre_key)


def is_newer(candidate: str, current: str) -> bool:
    return version_key(candidate) > version_key(current)
