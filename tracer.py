#!/usr/bin/env python3
"""Tracer subprocess: executes trusted user code under bdb and talks to the
parent Flask process over stdin (commands) / stdout (events), one JSON
object per line.

Protocol
--------
parent -> child:  {"code": "...", "breakpoints": [N, ...]} (first line, init)
parent -> child:  {"cmd": "step"|"continue"|"terminate"}
parent -> child:  {"cmd": "breakpoints", "breakpoints": [N, ...]}
child  -> parent: {"type": "pause", "line": N, "locals": {...}}
child  -> parent: {"type": "stdout", "data": "..."}
child  -> parent: {"type": "exception", "exc": "..."}
child  -> parent: {"type": "end"}

Pausing model
-------------
* "step" keeps bdb stepping one source line at a time (into calls and loop
  iterations); the first stop after launch is always the initial step stop.
* "continue" with no breakpoints is a plain bdb continue; with breakpoints
  it proceeds line-by-line (set_step) and only emits a pause on a source
  line whose number is in the active breakpoint set. Comment/blank lines
  never raise line events in bdb, so they never match. The line the run is
  paused on when continue is accepted cannot pause twice: bdb re-enters
  user_line only when a NEW line is reached, and a breakpoint line inside a
  loop pauses again on every later iteration as expected.
* {"cmd": "breakpoints", ...} is accepted only while the child is parked in
  user_line waiting for a command (i.e. the run is paused). It replaces the
  whole set in place, executes nothing and returns to the wait for the next
  step/continue command, so applying it neither advances code nor adds a
  pause snapshot.
"""

import bdb
import io
import json
import sys
import traceback

USER_FILENAME = "<user_code>"
MAX_REPR = 200

REAL_OUT = sys.stdout
REAL_IN = sys.stdin


def send(event):
    REAL_OUT.write(json.dumps(event, ensure_ascii=False) + "\n")
    REAL_OUT.flush()


class StdoutProxy(io.TextIOBase):
    """Redirects user prints into stdout events for the parent."""

    def write(self, s):
        if s:
            send({"type": "stdout", "data": s})
        return len(s)

    def flush(self):
        pass


def safe_repr(value):
    try:
        text = repr(value)
    except Exception:
        return "<unrepresentable>"
    if len(text) > MAX_REPR:
        text = text[:MAX_REPR] + "…"
    return text


def snapshot_locals(frame):
    result = {}
    for name, value in frame.f_locals.items():
        if name.startswith("__") and name.endswith("__"):
            continue
        result[name] = safe_repr(value)
    return result


def format_user_exc(exc):
    """Traceback limited to user-code frames plus the exception line."""
    tb = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ else []
    user_frames = [f for f in tb if f.filename == USER_FILENAME]
    parts = []
    if user_frames:
        parts.extend(traceback.format_list(user_frames))
    parts.extend(traceback.format_exception_only(type(exc), exc))
    return "".join(parts)


class Tracer(bdb.Bdb):
    def __init__(self, breakpoints=()):
        super().__init__()
        # Active source-line breakpoint set, owned by this child. Replaced
        # wholesale by {"cmd": "breakpoints"} while parked at a pause; the
        # running mode that interprets the set is selected separately in
        # wait_for_command via continue_until_breakpoint.
        self.breakpoints_set = set(breakpoints)
        # True between an accepted "continue" and the next pause/end: user
        # line events are filtered against breakpoints_set. Step mode keeps
        # bdb stepping and pauses on every source line.
        self.continue_until_breakpoint = False

    def user_line(self, frame):
        if frame.f_code.co_filename != USER_FILENAME:
            # Line inside stdlib etc.: keep stepping until back in user code.
            self.set_step()
            return
        if self.continue_until_breakpoint:
            if frame.f_lineno not in self.breakpoints_set:
                # Not a breakpoint line: do not talk to the parent, keep
                # line-stepping. Loop iterations re-enter here, so a
                # breakpoint line further down is re-evaluated every time.
                self.set_step()
                return
            # A breakpoint line is actually reached: pause before executing
            # it. Returning to step-filtering happens (or not) when the
            # parent answers with the next step/continue command.
            self.continue_until_breakpoint = False
        send({
            "type": "pause",
            "line": frame.f_lineno,
            "locals": snapshot_locals(frame),
        })
        self.wait_for_command()

    def wait_for_command(self):
        """Block until the parent sends a command that resumes execution.

        Breakpoint-set replacements are handled right here, any number of
        times: they mutate self.breakpoints_set and continue waiting without
        resuming the traced code (no history, no line advance)."""
        while True:
            raw = REAL_IN.readline()
            if not raw:
                raise SystemExit  # parent went away
            msg = json.loads(raw)
            cmd = msg.get("cmd")
            if cmd == "step":
                self.continue_until_breakpoint = False
                self.set_step()
                return
            if cmd == "continue":
                # With an empty breakpoint set this is a plain continue
                # (free run until end/exception); otherwise line-step while
                # filtering, which is what makes live breakpoint replacement
                # take effect on the next reached line.
                self.continue_until_breakpoint = bool(self.breakpoints_set)
                if self.breakpoints_set:
                    self.set_step()
                else:
                    self.set_continue()
                return
            if cmd == "breakpoints":
                self.breakpoints_set = set(msg.get("breakpoints") or [])
                continue
            if cmd == "terminate":
                raise SystemExit
            # Unknown command: stay conservative and single-step.
            self.continue_until_breakpoint = False
            self.set_step()
            return


def main():
    init = json.loads(REAL_IN.readline())
    code = init["code"]
    breakpoints = init.get("breakpoints") or []

    # Interactive input is not supported: stdin reads hit EOF immediately.
    sys.stdin = io.StringIO("")
    sys.stdout = StdoutProxy()
    sys.stderr = StdoutProxy()

    tracer = Tracer(breakpoints)
    tracer.set_step()  # stop at the first executable line (always)
    namespace = {"__name__": "__main__"}
    try:
        compiled = compile(code, USER_FILENAME, "exec")
        tracer.runctx(compiled, namespace, namespace)
    except SystemExit:
        pass
    except BaseException as exc:  # noqa: BLE001 - report anything to the UI
        send({"type": "exception", "exc": format_user_exc(exc)})
    finally:
        send({"type": "end"})


if __name__ == "__main__":
    main()
