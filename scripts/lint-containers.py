#!/usr/bin/env python3
# ruff: noqa: T201  # a CLI whose output (the findings) is stdout
r"""Lint what hadolint does not cover in the Dockerfiles and compose files.

  <ci>/scripts/lint-containers.py

<ci> is a checkout of Distant-Signal/ci; run it from the root of the
repository it checks (lint-scripts.py does).

Over every tracked Dockerfile and docker-compose*.yml it checks:

1. Every RUN shell body with ShellCheck at full strength (the repo's
   .shellcheckrc: every optional check, style severity). hadolint's
   embedded ShellCheck runs default checks only and ignores .shellcheckrc.
   The body is what the shell really gets: `RUN` and its `--flags` removed,
   `\` continuations kept, comment lines inside a continuation dropped (as
   Docker drops them), heredocs included (a RUN that is only `<<EOF` is the
   heredoc body itself, run by its shebang's shell if it has one). Exec-form
   RUNs, and RUNs under a SHELL that is not sh or bash, are skipped. The
   stage's ARG and ENV names (ENV inherited from a parent stage too) count
   as defined. SHELL sets the dialect.
2. The compose files' inline shell, with the same ShellCheck config:
   `healthcheck.test` (["CMD-SHELL", "<script>"] or a bare string, both
   `/bin/sh -c`) and `command` / `entrypoint` as `<sh|bash> -c <script>`
   (a list, or a string Compose splits shell-style). Compose interpolation
   is undone first: `$$` is a literal `$`; a single-`$` reference is
   substituted by Compose, so it becomes a plain word. The service's
   `environment:` keys count as defined.
3. Digest pins. Every image a build or Compose pulls must carry
   `@sha256:<digest>` (after its tag, so Renovate can still bump it):
   a Dockerfile's `# syntax=` directive, each `FROM`, and each external
   `COPY --from=` / `RUN --mount=...,from=`; each compose `image:` of a
   service without `build:`. A build-stage alias (`FROM builder`) and
   `scratch` need none. An image built from an ARG (`FROM ${BASE}`) or a
   compose variable (`${IMAGE:-default}`) must have a digest-pinned default.

Opt-outs, always with a reason:
- ShellCheck, Dockerfile: `# shellcheck disable=SCxxxx # reason` in the
  comment block directly above the RUN (it covers the whole RUN body).
- ShellCheck, compose: the same line at the start of the snippet.
- Digest pin, compose: `# lint-containers: unpinned-image # reason` on the
  line directly above the `image:` key.

Exit 1 if anything is reported. Needs PyYAML and shellcheck (both pinned in
pyproject.toml's `lint` dependency group).
"""

import dataclasses
import itertools
import json
import pathlib
import re
import shlex
import subprocess
import sys
from collections.abc import Iterator, Mapping, Sequence

import yaml

# The repository being checked: these scripts run from its root (the
# working directory), not from where this file lives (Distant-Signal/ci).
ROOT = pathlib.Path.cwd()
RCFILE = ROOT / ".shellcheckrc"
SHELLS = {"sh": "sh", "/bin/sh": "sh", "bash": "bash", "/bin/bash": "bash"}
PINNED = re.compile(r"@sha256:[0-9a-f]{64}$")
# Compose interpolation: `$$` escapes a literal `$`; `$VAR` / `${VAR...}` is
# Compose's own substitution.
COMPOSE_INTERPOLATION = re.compile(r"\$\$|\$\{[^}]*\}|\$[A-Za-z_][A-Za-z0-9_]*")
COMPOSE_DEFAULT = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*:?-(.*)\}$")
UNPINNED_OPT_OUT = "# lint-containers: unpinned-image"
# Dockerfile ARG references: $NAME, ${NAME}, ${NAME:-word}.
ARG_REF = re.compile(
    r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}|\$([A-Za-z_][A-Za-z0-9_]*)"
)
DIRECTIVE = re.compile(r"^#\s*([A-Za-z]+)\s*=\s*(.*?)\s*$")
HEREDOC = re.compile(r"<<(-?)([\"']?)([A-Za-z_][A-Za-z0-9_]*)\2")
RUN_FLAG = re.compile(r"--[a-z-]+=\S+")
SHELLCHECK_DIRECTIVE = re.compile(r"^#\s*shellcheck\s")


@dataclasses.dataclass
class Script:
    """A shell script to check and the source line of each of its lines."""

    path: str
    shell: str
    lines: list[tuple[int, str]]

    def text(self) -> str:
        """Return the script as ShellCheck reads it."""
        return "".join(f"{text}\n" for _, text in self.lines)


@dataclasses.dataclass
class Instruction:
    """One Dockerfile instruction, continuations and heredocs included."""

    keyword: str
    lines: list[tuple[int, str]]
    heredoc: list[tuple[int, str]]
    comments: list[str]

    @property
    def line(self) -> int:
        """Return the instruction's first line number."""
        return self.lines[0][0]

    def arguments(self) -> str:
        """Return the text after the keyword, continuations joined."""
        joined = " ".join(text.rstrip().removesuffix("\\") for _, text in self.lines)
        return joined.split(None, 1)[1] if len(joined.split(None, 1)) > 1 else ""


@dataclasses.dataclass
class Stage:
    """What a RUN in a build stage can see."""

    shell: str | None = "sh"
    env: set[str] = dataclasses.field(default_factory=set)
    args: set[str] = dataclasses.field(default_factory=set)


def tracked(*patterns: str) -> list[pathlib.Path]:
    """Return the tracked files matching the git pathspecs."""
    listed = subprocess.run(  # noqa: S603  # fixed argv
        ["git", "ls-files", "--", *patterns],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
    ).stdout.split()
    return [ROOT / name for name in listed]


def shellcheck(script: Script) -> list[str]:
    """Run ShellCheck over a script; return its findings, source-mapped."""
    # Fixed argv; the script only goes to shellcheck's stdin.
    result = subprocess.run(  # noqa: S603
        [  # noqa: S607  # the pinned shellcheck on PATH
            "shellcheck",
            f"--rcfile={RCFILE}",
            f"--shell={script.shell}",
            "--format=json1",
            "-",
        ],
        input=script.text(),
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode not in {0, 1}:
        return [f"{script.path}: shellcheck failed: {result.stderr.strip()}"]
    return [
        f"{script.path}:{script.lines[c['line'] - 1][0]}:{c['column']}: "
        f"{c['level']}: {c['message']} [SC{c['code']}]"
        for c in json.loads(result.stdout)["comments"]
    ]


def preamble(
    line: int, comments: Sequence[str], defined: set[str]
) -> list[tuple[int, str]]:
    """Return ShellCheck directives, then an `export` of the defined names.

    Directives before a script's first command apply to all of it.
    """
    lines = [(line, c) for c in comments if SHELLCHECK_DIRECTIVE.match(c)]
    if defined:
        lines.append((line, f"export {' '.join(sorted(defined))}"))
    return lines


# --- Dockerfiles ----------------------------------------------------------


def directives(text: str) -> dict[str, tuple[int, str]]:
    """Return a Dockerfile's parser directives (`# syntax=...`) by name."""
    found: dict[str, tuple[int, str]] = {}
    for number, line in enumerate(text.splitlines(), 1):
        match = DIRECTIVE.match(line)
        if match is None:
            break
        found[match.group(1).lower()] = (number, match.group(2))
    return found


def read_heredocs(
    lines: Sequence[str], start: int, body: Sequence[tuple[int, str]]
) -> tuple[list[tuple[int, str]], int]:
    """Collect the heredoc lines after an instruction; return them, next index."""
    heredoc: list[tuple[int, str]] = []
    i = start
    for match in HEREDOC.finditer("\n".join(text for _, text in body)):
        strip_tabs, word = match.group(1) == "-", match.group(3)
        while i < len(lines):
            heredoc.append((i + 1, lines[i]))
            i += 1
            end = lines[i - 1].lstrip("\t") if strip_tabs else lines[i - 1]
            if end == word:
                break
    return heredoc, i


def instructions(text: str) -> Iterator[Instruction]:
    r"""Yield a Dockerfile's instructions (default `\` escape only)."""
    lines = text.splitlines()
    comments: list[str] = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith("#"):
            comments = [*comments, stripped] if stripped else []
            i += 1
            continue
        body = [(i + 1, lines[i])]
        while body[-1][1].rstrip().endswith("\\") and i + 1 < len(lines):
            i += 1
            inner = lines[i].strip()
            if inner and not inner.startswith("#"):
                body.append((i + 1, lines[i]))
        keyword = body[0][1].split(None, 1)[0].upper()
        heredoc: list[tuple[int, str]] = []
        i += 1
        if keyword in {"RUN", "COPY", "ADD"}:
            heredoc, i = read_heredocs(lines, i, body)
        yield Instruction(keyword, body, heredoc, comments)
        comments = []


def blank(text: str, start: int, end: int) -> str:
    """Replace text[start:end] with spaces, keeping every column in place."""
    return text[:start] + " " * (end - start) + text[end:]


def strip_run_prefix(
    lines: Sequence[tuple[int, str]],
) -> tuple[list[tuple[int, str]], list[str]]:
    """Blank out `RUN` and its --flags; return the lines and the flags."""
    out = list(lines)
    flags: list[str] = []
    index = 0
    number, text = out[0]
    pos = len(text) - len(text.lstrip()) + len("RUN")
    text = blank(text, 0, pos)
    while True:
        pos += len(text[pos:]) - len(text[pos:].lstrip())
        if text[pos:].rstrip() == "\\":
            out[index] = (number, blank(text, pos, len(text)))
            index += 1
            if index == len(out):
                return out, flags
            number, text = out[index]
            pos = 0
            continue
        flag = RUN_FLAG.match(text, pos)
        if flag is None:
            break
        flags.append(flag.group())
        text = blank(text, pos, flag.end())
        pos = flag.end()
    out[index] = (number, text)
    return out, flags


def run_script(
    path: str, instruction: Instruction, stage: Stage
) -> tuple[Script | None, list[str]]:
    """Return a RUN's shell script (None if not shell) and its --flags."""
    lines, flags = strip_run_prefix(instruction.lines)
    command = "\n".join(text for _, text in lines).strip()
    if stage.shell is None or command.startswith("["):
        return None, flags
    shell = stage.shell
    body = lines + instruction.heredoc
    if HEREDOC.fullmatch(command):
        # The heredoc is the script; its shebang (if any) picks the shell.
        body = instruction.heredoc[:-1]
        if body and body[0][1].startswith("#!"):
            interpreter = body[0][1][2:].split()
            program = (
                interpreter[1] if interpreter[0].endswith("/env") else interpreter[0]
            )
            name = pathlib.PurePath(program).name
            if name not in SHELLS:
                return None, flags
            shell = SHELLS[name]
            body = body[1:]
    defined = stage.env | stage.args
    script = preamble(instruction.line, instruction.comments, defined) + body
    return Script(path, shell, script), flags


def resolve_args(image: str, args: Mapping[str, str]) -> str | None:
    """Substitute global ARG defaults into an image; None if one is missing."""
    missing = False

    def substitute(match: re.Match[str]) -> str:
        nonlocal missing
        name = match.group(1) or match.group(3)
        value = args.get(name) or match.group(2) or ""
        missing = missing or not value
        return value

    resolved = ARG_REF.sub(substitute, image)
    return None if missing else resolved


def check_image(where: str, image: str, args: Mapping[str, str]) -> list[str]:
    """Return a finding if an image reference is not digest-pinned."""
    resolved = resolve_args(image, args)
    if resolved is None:
        return [f"{where}: {image}: an ARG it uses has no default"]
    if not PINNED.search(resolved):
        return [f"{where}: {image}: not pinned by @sha256 digest"]
    return []


def arg_names(arguments: str) -> dict[str, str]:
    """Return ARG names and defaults ("" if none)."""
    return {
        token.partition("=")[0]: token.partition("=")[2]
        for token in shlex.split(arguments)
    }


def mount_from(flag: str) -> str:
    """Return a `--mount=...,from=<ref>` flag's ref, else ""."""
    if not flag.startswith("--mount="):
        return ""
    options = flag.removeprefix("--mount=").split(",")
    return dict(o.partition("=")[::2] for o in options).get("from", "")


def env_names(arguments: str) -> set[str]:
    """Return the names an ENV instruction sets (both ENV syntaxes)."""
    tokens = shlex.split(arguments)
    if tokens and "=" not in tokens[0]:
        return {tokens[0]}
    return {token.split("=", 1)[0] for token in tokens}


class Dockerfile:
    """Walk one Dockerfile's stages, collecting scripts and digest findings."""

    def __init__(self, path: pathlib.Path) -> None:
        """Parse the file."""
        self.name = str(path.relative_to(ROOT))
        self.text = path.read_text()
        self.global_args: dict[str, str] = {}
        self.stages: dict[str, Stage] = {}
        self.stage: Stage | None = None
        self.scripts: list[Script] = []
        self.findings: list[str] = []

    def external(self, ref: str) -> bool:
        """Say whether a --from value names an image, not a stage."""
        return ref.lower() not in self.stages and not ref.isdigit()

    def on_from(self, instruction: Instruction) -> None:
        """Start a new stage; check its base image."""
        tokens = [
            t for t in shlex.split(instruction.arguments()) if not t.startswith("--")
        ]
        image = tokens[0]
        parent = self.stages.get(image.lower())
        where = f"{self.name}:{instruction.line}"
        if parent is None and image != "scratch":
            self.findings += check_image(where, image, self.global_args)
        self.stage = Stage(
            shell=parent.shell if parent else "sh",
            env=set(parent.env) if parent else set(),
        )
        self.stages[str(len(self.stages))] = self.stage
        if len(tokens) >= 3 and tokens[1].lower() == "as":  # noqa: PLR2004
            self.stages[tokens[2].lower()] = self.stage

    def on_instruction(self, instruction: Instruction, stage: Stage) -> None:
        """Track ARG/ENV/SHELL; lint RUN; check RUN/COPY/ADD --from images."""
        arguments = instruction.arguments()
        refs: list[str] = []
        if instruction.keyword == "ARG":
            stage.args |= set(arg_names(arguments))
        elif instruction.keyword == "ENV":
            stage.env |= env_names(arguments)
        elif instruction.keyword == "SHELL":
            stage.shell = SHELLS.get(json.loads(arguments)[0])
        elif instruction.keyword == "RUN":
            script, flags = run_script(self.name, instruction, stage)
            if script is not None:
                self.scripts.append(script)
            refs = [mount_from(flag) for flag in flags]
        elif instruction.keyword in {"COPY", "ADD"}:
            refs = [
                token.removeprefix("--from=")
                for token in shlex.split(arguments)
                if token.startswith("--from=")
            ]
        for ref in refs:
            if ref and self.external(ref):
                where = f"{self.name}:{instruction.line}"
                self.findings += check_image(where, ref, self.global_args)

    def walk(self) -> None:
        """Process every instruction."""
        syntax = directives(self.text).get("syntax")
        if syntax is not None:
            self.findings += check_image(f"{self.name}:{syntax[0]}", syntax[1], {})
        for instruction in instructions(self.text):
            if instruction.keyword == "FROM":
                self.on_from(instruction)
            elif self.stage is None:
                if instruction.keyword == "ARG":
                    self.global_args |= arg_names(instruction.arguments())
            else:
                self.on_instruction(instruction, self.stage)


# --- Compose files --------------------------------------------------------


def node_get(node: yaml.Node | None, key: str) -> yaml.Node | None:
    """Return a mapping node's value for a key."""
    if isinstance(node, yaml.MappingNode):
        for key_node, value in node.value:
            if isinstance(key_node, yaml.ScalarNode) and key_node.value == key:
                return value  # type: ignore[no-any-return]
    return None


def scalars(node: yaml.Node | None) -> list[str] | str | None:
    """Return a scalar's string, a sequence's strings, else None."""
    if isinstance(node, yaml.ScalarNode):
        return str(node.value)
    if isinstance(node, yaml.SequenceNode):
        return [str(item.value) for item in node.value]
    return None


def compose_shell_text(script: str) -> str:
    """Return the text the shell receives once Compose has interpolated."""
    return COMPOSE_INTERPOLATION.sub(
        lambda m: "$" if m.group() == "$$" else "compose_interpolated", script
    )


def shell_c(argv: list[str] | str | None) -> tuple[str, str] | None:
    """Return (shell, script) if argv is `<sh|bash> -c <script>`, else None."""
    if isinstance(argv, str):
        argv = shlex.split(argv)
    if argv and len(argv) >= 3 and argv[0] in SHELLS and argv[1] == "-c":  # noqa: PLR2004
        return SHELLS[argv[0]], argv[2]
    return None


def environment(service: yaml.Node) -> set[str]:
    """Return the variable names a service's `environment:` sets."""
    env = node_get(service, "environment")
    if isinstance(env, yaml.MappingNode):
        return {str(key.value) for key, _ in env.value}
    return {item.split("=", 1)[0] for item in scalars(env) or []}


def service_snippets(service: yaml.Node) -> Iterator[tuple[str, yaml.Node, str, str]]:
    """Yield (key, node, shell, script) for a service's inline shell."""
    test = node_get(node_get(service, "healthcheck"), "test")
    if isinstance(test, yaml.ScalarNode):
        yield "healthcheck.test", test, "sh", str(test.value)
    elif isinstance(test, yaml.SequenceNode) and scalars(test)[:1] == ["CMD-SHELL"]:  # type: ignore[index]
        yield "healthcheck.test", test.value[1], "sh", str(test.value[1].value)
    for key in ("entrypoint", "command"):
        node = node_get(service, key)
        found = shell_c(scalars(node))
        if node is not None and found is not None:
            at = node.value[2] if isinstance(node, yaml.SequenceNode) else node
            yield key, at, *found


class Compose:
    """Collect one compose file's inline shell and digest findings."""

    def __init__(self, path: pathlib.Path) -> None:
        """Parse the file."""
        self.name = str(path.relative_to(ROOT))
        self.raw = path.read_text().splitlines()
        self.root = yaml.compose(path.read_text(), Loader=yaml.SafeLoader)
        self.scripts: list[Script] = []
        self.findings: list[str] = []

    def services(self) -> Iterator[tuple[yaml.Node, yaml.Node]]:
        """Yield (name node, service node) pairs."""
        services = node_get(self.root, "services")
        if isinstance(services, yaml.MappingNode):
            yield from services.value

    def check_image(self, service: yaml.Node) -> None:
        """Report a service's `image:` if it lacks a digest (and no opt-out)."""
        image = node_get(service, "image")
        if image is None or node_get(service, "build") is not None:
            return
        line = image.start_mark.line
        above = self.raw[line - 1].strip() if line else ""
        if above.startswith(UNPINNED_OPT_OUT):
            return
        value = str(image.value)
        default = COMPOSE_DEFAULT.match(value)
        pinned = default.group(1) if default else value
        if "$" in pinned or not PINNED.search(pinned):
            self.findings.append(
                f"{self.name}:{line + 1}: {value}: not pinned by @sha256 digest"
            )

    def walk(self) -> None:
        """Process every service."""
        for name, service in self.services():
            self.check_image(service)
            env = environment(service)
            for key, node, shell, script in service_snippets(service):
                # Block scalars start on the line after their indicator.
                block = getattr(node, "style", None) in {"|", ">"}
                first = node.start_mark.line + 1 + block
                body = compose_shell_text(script).splitlines()
                # Leading directives go ahead of the `export` line so they
                # still apply to the whole snippet.
                leading = [
                    line.strip()
                    for line in itertools.takewhile(
                        lambda line: SHELLCHECK_DIRECTIVE.match(line.strip()), body
                    )
                ]
                self.scripts.append(
                    Script(
                        f"{self.name} (services.{name.value}.{key})",
                        shell,
                        preamble(first, leading, env)
                        + [(first + n, text) for n, text in enumerate(body)],
                    )
                )


def main() -> int:
    """Lint every Dockerfile and compose file; return 1 on any finding."""
    walkers: list[Dockerfile | Compose] = [
        *(Dockerfile(p) for p in tracked("*Dockerfile")),
        *(Compose(p) for p in tracked("docker-compose*.yml", "compose*.yml")),
    ]
    findings: list[str] = []
    count = 0
    for walker in walkers:
        walker.walk()
        findings += walker.findings
        for script in walker.scripts:
            count += 1
            findings += shellcheck(script)
    for finding in findings:
        print(finding)
    print(f"{len(walkers)} file(s), {count} shell script(s) checked")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
