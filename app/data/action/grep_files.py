from agent_core import action

_INPUT_SCHEMA = {
    "pattern": {
        "type": "string",
        "example": "def \\w+\\(",
        "description": "Regex to search for (ripgrep syntax). Escape regex characters such as ( [ . to match them literally.",
    },
    "path": {
        "type": "string",
        "example": "/workspace/project",
        "description": "File or folder to search. Defaults to the current working directory.",
    },
    "glob": {
        "type": "string",
        "example": "*.{ts,tsx}",
        "description": "Optional. Only search files matching this glob, e.g. '*.py', '*.{ts,tsx}' or 'src/**/*.ts'.",
    },
    "ignore_case": {
        "type": "boolean",
        "example": False,
        "description": "Case-insensitive search. Default false.",
    },
    "context": {
        "type": "integer",
        "example": 0,
        "description": "Lines to show before and after each match. Default 0.",
    },
    "limit": {
        "type": "integer",
        "example": 100,
        "description": "Maximum number of matches to return. Default 100.",
    },
}

_OUTPUT_SCHEMA = {
    "status": {
        "type": "string",
        "example": "success",
        "description": "'success' or 'error'.",
    },
    "message": {
        "type": "string",
        "example": "Found 2 matches in 2 files under /workspace/project; paths are relative to it.",
        "description": "What was searched and found, including why a result may be incomplete.",
    },
    "content": {
        "type": "string",
        "example": "src/main.py:10: def hello():\nsrc/util.py:4: def world():",
        "description": "Matching lines as 'path:line: text'. Context lines read 'path-line- text' and non-adjacent groups are separated by '--'.",
    },
    "num_matches": {
        "type": "integer",
        "example": 2,
        "description": "Number of matching lines in content.",
    },
    "num_files": {
        "type": "integer",
        "example": 2,
        "description": "Number of files those matches come from.",
    },
    "truncated": {
        "type": "boolean",
        "example": False,
        "description": "True when more matches exist than content shows.",
    },
}


@action(
    name="grep_files",
    description=(
        "Search file contents with a ripgrep regex. Returns matching lines as "
        "'path:line: text', with paths relative to the searched folder. "
        "Searching a folder skips files ignored by .gitignore, hidden files and "
        "binary files. Returns at most `limit` matches. Use read_file on a "
        "matched file when you need more of it."
    ),
    mode="CLI",
    platforms=["linux", "windows", "darwin"],
    action_sets=["core"],
    input_schema=_INPUT_SCHEMA,
    output_schema=_OUTPUT_SCHEMA,
    test_payload={
        "pattern": "Mt\\. Fuji|visibility",
        "path": "/path/to",
        "ignore_case": True,
        "limit": 50,
        "simulated_mode": True,
    },
)
def grep_files(input_data: dict) -> dict:
    """Search files with ripgrep and return the matching lines."""
    import base64
    import json
    import os
    import subprocess
    import threading

    from agent_core.core.impl.action.cancellation import (
        register_process,
        unregister_process,
    )
    from app import ripgrep_runtime

    # One matched line of a minified bundle can be megabytes, and the whole
    # result lands in the event stream: both caps keep one search from
    # flooding the context.
    MAX_LINE_CHARS = 500
    MAX_CONTENT_CHARS = 40000
    DEFAULT_LIMIT = 100

    def make_result(
        status, message, content="", num_matches=0, num_files=0, truncated=False
    ):
        return {
            "status": status,
            "message": message,
            "content": content,
            "num_matches": num_matches,
            "num_files": num_files,
            "truncated": truncated,
        }

    def as_int(value, default):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    if input_data.get("simulated_mode", False):
        return make_result(
            "success",
            "Found 2 matches in 2 files under /path/to; paths are relative to it.",
            "input.txt:10: Mt. Fuji is visible today\nother.txt:5: visibility is low",
            num_matches=2,
            num_files=2,
        )

    pattern = input_data.get("pattern")
    if not isinstance(pattern, str) or pattern == "":
        return make_result("error", "pattern is required.")

    root = os.path.abspath(os.path.expanduser(input_data.get("path") or os.getcwd()))
    if not os.path.exists(root):
        return make_result("error", f"Path does not exist: {root}")
    root_is_dir = os.path.isdir(root)

    glob = input_data.get("glob") or None
    ignore_case = bool(input_data.get("ignore_case", False))
    context = max(0, as_int(input_data.get("context"), 0))
    limit = max(1, as_int(input_data.get("limit"), DEFAULT_LIMIT))

    try:
        rg = ripgrep_runtime.resolve()
    except RuntimeError as e:
        return make_result("error", f"Cannot search: {e}")

    # --no-require-git: a project's .gitignore applies whether or not the
    # folder sits inside a git checkout, so a search skips the same files in
    # a dev checkout and in an installed copy.
    # --no-messages: unreadable files are skipped silently; a bad regex is
    # still reported on stderr.
    args = [rg, "--no-config", "--json", "--no-messages", "--no-require-git"]
    if ignore_case:
        args.append("--ignore-case")
    if context:
        args += ["--context", str(context)]
    if glob and root_is_dir:
        args += ["--glob", glob]
    args += ["--regexp", pattern, "--", root]

    def text_of(field):
        # rg sends a line that is not valid UTF-8 as base64 "bytes".
        if "text" in field:
            return field["text"]
        return base64.b64decode(field.get("bytes", "")).decode("utf-8", "replace")

    # (path, line number, text, is_match) in rg's output order.
    records = []
    match_count = 0
    limit_line = None
    stderr_parts = []
    session_id = input_data.get("_session_id") or ""

    try:
        proc = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except OSError as e:
        return make_result("error", f"Could not start ripgrep at {rg}: {e}")

    # A user force-stop kills rg; reading stdout then simply ends.
    register_process(session_id, proc)
    # stderr is drained on its own thread: a full pipe would block rg while
    # this thread waits on stdout.
    drain = threading.Thread(
        target=lambda: stderr_parts.append(proc.stderr.read()), daemon=True
    )
    drain.start()
    try:
        for raw in proc.stdout:
            record = json.loads(raw)
            kind = record.get("type")
            if kind not in ("match", "context"):
                if limit_line is not None and kind in ("begin", "end"):
                    break
                continue
            data = record["data"]
            line_no = data["line_number"]
            if limit_line is not None:
                # Past the limit, keep only the trailing context of the last
                # match, never the lead-in of the next one.
                if kind == "match" or line_no > limit_line + context:
                    break
            records.append(
                (
                    text_of(data["path"]),
                    line_no,
                    text_of(data["lines"]).rstrip("\r\n"),
                    kind == "match",
                )
            )
            if kind == "match":
                match_count += 1
                if match_count >= limit:
                    limit_line = line_no
                    if not context:
                        break
    except Exception as e:
        proc.kill()
        return make_result("error", f"Search failed: {e}")
    finally:
        if proc.poll() is None:
            proc.kill()
        returncode = proc.wait()
        drain.join(timeout=5)
        unregister_process(session_id, proc)

    limit_hit = limit_line is not None
    stderr_text = (
        b"".join(p for p in stderr_parts if p).decode("utf-8", "replace").strip()
    )

    # rg exits 2 on any error, including one unreadable file in a tree that
    # otherwise matched; it is only a failure when nothing usable came back.
    if not records and not limit_hit and returncode not in (0, 1):
        return make_result(
            "error",
            f"Search failed: {stderr_text or f'ripgrep exited with code {returncode}'}",
        )

    if not records:
        if root_is_dir:
            message = (
                f"No matches under {root}. Searching a folder skips files ignored "
                f"by .gitignore, hidden files and binary files."
            )
        else:
            message = f"No matches in {root}."
        return make_result("success", message)

    def display(path):
        if not root_is_dir:
            return os.path.basename(path)
        normalized = os.path.normpath(path)
        try:
            relative = os.path.relpath(normalized, root)
        except ValueError:  # another drive on Windows
            return normalized.replace(os.sep, "/")
        if relative.startswith(".."):
            return normalized.replace(os.sep, "/")
        return relative.replace(os.sep, "/")

    lines = []
    used = 0
    shown_matches = 0
    shown_files = set()
    lines_cut = 0
    capped = False
    previous = None
    for path, line_no, text, is_match in records:
        block = []
        if (
            context
            and previous is not None
            and (previous[0] != path or line_no != previous[1] + 1)
        ):
            block.append("--")
        if len(text) > MAX_LINE_CHARS:
            text = text[:MAX_LINE_CHARS] + " [line cut]"
            lines_cut += 1
        sep = ":" if is_match else "-"
        block.append(f"{display(path)}{sep}{line_no}{sep} {text}")
        size = sum(len(b) + 1 for b in block)
        if used + size > MAX_CONTENT_CHARS:
            capped = True
            break
        lines.extend(block)
        used += size
        previous = (path, line_no)
        if is_match:
            shown_matches += 1
            shown_files.add(path)

    where = f"under {root}; paths are relative to it" if root_is_dir else f"in {root}"
    notes = [
        f"Found {shown_matches} match{'' if shown_matches == 1 else 'es'} in "
        f"{len(shown_files)} file{'' if len(shown_files) == 1 else 's'} {where}."
    ]
    if limit_hit:
        notes.append(
            f"Stopped at the limit of {limit} matches; more may exist. Raise "
            f"limit or narrow the pattern, path or glob."
        )
    if capped:
        notes.append(
            f"Output capped at {MAX_CONTENT_CHARS} characters; narrow the search "
            f"to see the rest."
        )
    if lines_cut:
        notes.append(
            f"{lines_cut} long line{'' if lines_cut == 1 else 's'} cut to "
            f"{MAX_LINE_CHARS} characters; read_file shows them in full."
        )
    if returncode == 2 and not limit_hit:
        notes.append("Some files could not be read.")

    return make_result(
        "success",
        " ".join(notes),
        "\n".join(lines),
        num_matches=shown_matches,
        num_files=len(shown_files),
        truncated=limit_hit or capped,
    )
