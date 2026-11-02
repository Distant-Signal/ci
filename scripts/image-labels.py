#!/usr/bin/env python3
# ruff: noqa: T201  # a CLI whose answer is stdout
"""Build the label list image.yml passes to docker/build-push-action.

  python3 <ci>/scripts/image-labels.py --title T --source URL
      --licenses SPDX [--extra-file FILE]

Prints the image's labels, one `key=value` per line: first image.yml's own
(org.opencontainers.image.title, .source, .url and .licenses), then the
caller's extra labels, read from FILE (image.yml writes its `labels` input
there; `-` or no file means none). With $GITHUB_OUTPUT set, it also writes
them there as the multi-line output `labels`.

The extra labels are the calling repository's own, e.g. the repo split
spec's org.distant-signal.core-version and org.distant-signal.api-build
(§5.1, §5.3). Blank lines and surrounding spaces are dropped. A line is
refused (exit 2, one `::error::` line each) when it is not `key=value`, when
its key is not lower-case dotted (`[a-z0-9]` words joined by `.`, `-` or
`_`, as the OCI annotation rules recommend), when its value is empty, when
it repeats a key, or when it sets one of image.yml's own labels: those come
from the inputs (`title-prefix`, `licenses`) and the calling repository, so
an extra label can't contradict them.

Labels are part of the image config, so a label whose value changes on
every run (a run number, a commit) gives every build a new digest, and the
image's Deployment restarts on every deploy. Prefer values that change only
when what they describe does.

Stdlib only.
"""

import argparse
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path

OWN = (
    "org.opencontainers.image.title",
    "org.opencontainers.image.source",
    "org.opencontainers.image.url",
    "org.opencontainers.image.licenses",
)
KEY = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*")
DELIMITER = "DS_CI_IMAGE_LABELS_EOF"


class LabelError(ValueError):
    """An extra label image.yml refuses."""


def parse_extra(text: str) -> list[tuple[str, str]]:
    """Return TEXT's labels in order; raise LabelError naming every bad line."""
    labels: list[tuple[str, str]] = []
    seen: set[str] = set()
    problems: list[str] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep:
            problems.append(f"line {number}: {line!r} is not key=value")
        elif not KEY.fullmatch(key):
            problems.append(f"line {number}: {key!r} is not a lower-case dotted key")
        elif not value:
            problems.append(f"line {number}: {key} has an empty value")
        elif key in OWN:
            problems.append(
                f"line {number}: {key} is image.yml's own label; set it with "
                "image.yml's inputs"
            )
        elif key in seen:
            problems.append(f"line {number}: {key} is set twice")
        elif DELIMITER in value:
            problems.append(f"line {number}: {key}'s value holds {DELIMITER}")
        else:
            seen.add(key)
            labels.append((key, value))
    if problems:
        raise LabelError("\n".join(problems))
    return labels


def all_labels(
    title: str, source: str, licenses: str, extra: str
) -> list[tuple[str, str]]:
    """image.yml's own labels, then the extra ones."""
    own = list(zip(OWN, (title, source, source, licenses), strict=True))
    return own + parse_extra(extra)


def main(argv: Sequence[str] | None = None) -> int:
    """Print (and output) the labels; 2 on a refused extra label."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--title", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--licenses", required=True)
    parser.add_argument("--extra-file", default="-")
    args = parser.parse_args(argv)
    extra = "" if args.extra_file == "-" else Path(args.extra_file).read_text("utf-8")
    try:
        labels = all_labels(args.title, args.source, args.licenses, extra)
    except LabelError as error:
        for problem in str(error).splitlines():
            print(f"::error::labels input, {problem}")
        return 2
    lines = [f"{key}={value}" for key, value in labels]
    print("\n".join(lines))
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as out:
            out.write(f"labels<<{DELIMITER}\n" + "\n".join(lines) + f"\n{DELIMITER}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
