import asyncio
import codecs
import contextlib
import dataclasses
import pathlib
import subprocess
import sys
from collections.abc import Callable, Generator, Mapping
from typing import TextIO


@dataclasses.dataclass
class ProcessResult:
    """
    Result of a finished process.

    In PTY mode stdout and stderr are merged by the terminal, so only `combined_out` is filled.
    """

    exit_code: int
    stdout: str
    stderr: str
    combined_out: str


class LineBuilder:
    def __init__(self) -> None:
        self.buffer = ""

    def feed(self, chunk: str) -> Generator[str]:
        self.buffer += chunk
        while "\n" in self.buffer:
            index = self.buffer.index("\n")
            line = self.buffer[:index]
            self.buffer = self.buffer[index + 1 :]
            yield line

    def flush(self) -> str:
        """Return the remaining unterminated line and clear the buffer."""
        line, self.buffer = self.buffer, ""
        return line


def start_process(
    command: str | pathlib.Path,
    args: list[str | pathlib.Path] | None = None,
    *,
    cwd: str | pathlib.Path | None = None,
    env: Mapping[str, str] | None = None,
    on_line_callback: Callable[[str], None] | None = None,
    print_stdout: bool = True,
    print_stderr: bool = True,
    capture_stdout: bool = False,
    capture_stderr: bool = False,
    raise_on_exitcode: bool = False,
    pty: bool = False,
    wait: bool = True,
    new_console: bool = False,
) -> ProcessResult | None:
    """
    Synchronously start a subprocess.

    Args:
        command: The command to execute (e.g., 'python').
        args: A list of arguments for the command.
        cwd: Set current working directory of the process to the given path.
        env: Environment variables of the process, replacing the inherited ones. Inherits current environment if None.
        on_line_callback: Optional callback function to call for each line of output.
        print_stdout: Whether to print output to stdout
        print_stderr: Whether to print output to stderr
        capture_stdout: Whether to store stdout in return value
        capture_stderr: Whether to store stderr in return value
        raise_on_exitcode: If True, raises CalledProcessError for non-zero exit codes.
        pty: Whether to run process in pseudo-terminal.
        wait: If True, waits for process completion; if False, returns immediately.
        new_console: If True, starts process in a new detached console window (Windows only).

    Output related arguments and `pty` are ignored if `new_console` is True or `wait` is False,
    as the process output is not intercepted in that case.

    Returns:
        ProcessResult: The return code and output of the subprocess.
        Or None if `wait` is False.

    Raises:
        subprocess.CalledProcessError: On non-zero exit code if `raise_on_exitcode` is True.
    """
    command_str = str(command)
    args_str = [str(a) for a in args or []]
    cwd_str = None if cwd is None else str(cwd)

    if new_console or not wait:
        return _start_subprocess(
            command_str,
            args_str,
            cwd=cwd_str,
            env=env,
            raise_on_exitcode=raise_on_exitcode,
            wait=wait,
            new_console=new_console,
        )
    if pty:
        pty_start_process = _win_pty_start_process if sys.platform == "win32" else _unix_pty_start_process
        return pty_start_process(
            command_str,
            args_str,
            cwd=cwd_str,
            env=env,
            on_line_callback=on_line_callback,
            print_output=print_stdout or print_stderr,
            capture_output=capture_stdout or capture_stderr,
            raise_on_exitcode=raise_on_exitcode,
        )
    return asyncio.run(
        _async_start_process(
            command_str,
            args_str,
            cwd=cwd_str,
            env=env,
            on_line_callback=on_line_callback,
            print_stdout=print_stdout,
            print_stderr=print_stderr,
            capture_stdout=capture_stdout,
            capture_stderr=capture_stderr,
            raise_on_exitcode=raise_on_exitcode,
        )
    )


def _win_pty_start_process(
    command: str,
    args: list[str],
    *,
    cwd: str | None = None,
    env: Mapping[str, str] | None,
    on_line_callback: Callable[[str], None] | None,
    print_output: bool,
    capture_output: bool,
    raise_on_exitcode: bool,
) -> ProcessResult:
    import msvcrt
    import os
    import shutil
    import signal
    import threading
    import time

    import winpty

    command = shutil.which(command) or command
    cmdline = subprocess.list2cmdline([command] + args)
    # Unlike os.get_terminal_size(), it falls back to a default size if stdout is not a terminal
    term_size = shutil.get_terminal_size()
    pty = winpty.PTY(term_size.columns, term_size.lines, backend=winpty.Backend.WinPTY)
    # winpty expects environment as a block of "name=value" pairs, each terminated by a null character
    env_block = None if env is None else "".join(f"{key}={value}\0" for key, value in env.items())
    pty.spawn(command, cmdline=cmdline, cwd=cwd, env=env_block)

    def input_thread_func() -> None:
        with contextlib.suppress(OSError):
            while pty.isalive():
                if msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    try:
                        pty.write(ch)
                    except (BrokenPipeError, OSError):
                        break
                else:
                    time.sleep(0.1)

    if sys.stdin.isatty():
        input_thread = threading.Thread(target=input_thread_func, daemon=True)
        input_thread.start()

    handler = _OutputHandler(
        sys.stdout, print_output=print_output, capture_output=capture_output, on_line_callback=on_line_callback
    )

    try:
        while True:
            try:
                data = pty.read(blocking=True)
            except winpty.WinptyError:
                break

            if not data:
                if pty.iseof():
                    break
                continue

            handler.feed(data)

        while pty.isalive():
            time.sleep(0.1)
    except KeyboardInterrupt:
        if pty.pid:
            os.kill(pty.pid, signal.SIGTERM)
        raise

    handler.close()

    result = ProcessResult(
        exit_code=pty.get_exitstatus() or 0,
        stdout="",
        stderr="",
        combined_out=handler.output,
    )

    if raise_on_exitcode and result.exit_code != 0:
        raise subprocess.CalledProcessError(result.exit_code, [command] + args, output=result.combined_out, stderr=None)

    return result


def _unix_pty_start_process(
    command: str,
    args: list[str],
    *,
    cwd: str | None = None,
    env: Mapping[str, str] | None,
    on_line_callback: Callable[[str], None] | None,
    print_output: bool,
    capture_output: bool,
    raise_on_exitcode: bool,
) -> ProcessResult:
    import shutil

    import pexpect
    import pexpect.pty_spawn

    term_size = shutil.get_terminal_size()
    proc = pexpect.pty_spawn.spawn(
        command,
        args,
        cwd=cwd,
        env=env,
        encoding="utf-8",
        codec_errors="replace",
        timeout=None,
        dimensions=(term_size.lines, term_size.columns),
    )

    handler = _OutputHandler(
        sys.stdout, print_output=print_output, capture_output=capture_output, on_line_callback=on_line_callback
    )

    try:
        while True:
            try:
                data = proc.read_nonblocking(size=4096, timeout=None)
            except pexpect.EOF:
                break
            handler.feed(data)
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate(force=True)
        raise

    handler.close()

    # Use subprocess convention of negative exit code for processes killed by a signal
    if proc.exitstatus is not None:
        exit_code = proc.exitstatus
    else:
        exit_code = -(proc.signalstatus or 0)

    result = ProcessResult(
        exit_code=exit_code,
        stdout="",
        stderr="",
        combined_out=handler.output,
    )

    if raise_on_exitcode and result.exit_code != 0:
        raise subprocess.CalledProcessError(result.exit_code, [command] + args, output=result.combined_out, stderr=None)

    return result


async def _async_start_process(
    command: str,
    args: list[str],
    *,
    cwd: str | None = None,
    env: Mapping[str, str] | None,
    on_line_callback: Callable[[str], None] | None,
    print_stdout: bool = True,
    print_stderr: bool = True,
    capture_stdout: bool = False,
    capture_stderr: bool = False,
    raise_on_exitcode: bool,
) -> ProcessResult:
    process = await asyncio.create_subprocess_exec(
        command,
        *args,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdout is not None
    assert process.stderr is not None

    combined_chunks: list[str] = []
    stdout_handler = _OutputHandler(
        sys.stdout,
        print_output=print_stdout,
        capture_output=capture_stdout,
        on_line_callback=on_line_callback,
        combined_chunks=combined_chunks,
    )
    stderr_handler = _OutputHandler(
        sys.stderr,
        print_output=print_stderr,
        capture_output=capture_stderr,
        on_line_callback=on_line_callback,
        combined_chunks=combined_chunks,
    )

    async def pump(stream: asyncio.StreamReader, handler: _OutputHandler) -> None:
        # Incremental decoder keeps multibyte characters split between chunks intact
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while chunk := await stream.read(4096):
            handler.feed(decoder.decode(chunk))
        handler.feed(decoder.decode(b"", final=True))
        handler.close()

    try:
        await asyncio.gather(pump(process.stdout, stdout_handler), pump(process.stderr, stderr_handler))
        exit_code = await process.wait()
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        raise

    result = ProcessResult(
        exit_code=exit_code,
        stdout=stdout_handler.output,
        stderr=stderr_handler.output,
        combined_out="".join(combined_chunks),
    )

    if raise_on_exitcode and result.exit_code != 0:
        raise subprocess.CalledProcessError(
            result.exit_code, [command] + args, output=result.stdout, stderr=result.stderr
        )

    return result


def _start_subprocess(
    command: str,
    args: list[str],
    *,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
    raise_on_exitcode: bool = False,
    wait: bool = True,
    new_console: bool = False,
) -> ProcessResult | None:
    creation_flags = 0
    if new_console:
        creation_flags |= subprocess.CREATE_NEW_CONSOLE
    process = subprocess.Popen([command] + args, cwd=cwd, env=env, creationflags=creation_flags)
    if not wait:
        return None
    exit_code = process.wait()
    if raise_on_exitcode and exit_code != 0:
        raise subprocess.CalledProcessError(exit_code, [command] + args, output="", stderr="")
    return ProcessResult(exit_code, "", "", "")


class _OutputHandler:
    """Print, capture and split into lines the decoded output of a single stream."""

    def __init__(
        self,
        out: TextIO,
        *,
        print_output: bool,
        capture_output: bool,
        on_line_callback: Callable[[str], None] | None,
        combined_chunks: list[str] | None = None,
    ) -> None:
        self._out = out
        self._print_output = print_output
        self._capture_output = capture_output
        self._on_line_callback = on_line_callback
        self._combined_chunks = combined_chunks
        self._line_builder = LineBuilder()
        self._chunks: list[str] = []

    @property
    def output(self) -> str:
        return "".join(self._chunks)

    def feed(self, text: str) -> None:
        if not text:
            return
        if self._print_output:
            self._out.write(text)
            self._out.flush()
        if self._capture_output:
            self._chunks.append(text)
            if self._combined_chunks is not None:
                self._combined_chunks.append(text)
        if self._on_line_callback:
            for line in self._line_builder.feed(text):
                self._on_line_callback(line)

    def close(self) -> None:
        """Pass the last unterminated line, if any, to the callback."""
        if self._on_line_callback and (line := self._line_builder.flush()):
            self._on_line_callback(line)
