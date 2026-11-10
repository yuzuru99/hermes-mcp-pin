"""Type slash commands into an interactive ``hermes chat`` (POSIX pty) and print the plain-text screen."""
import os
import pty
import re
import select
import sys
import time


# First words of every /mcppin reply.
REPLIES = [m.encode() for m in ("held definition", "Approved and pinned", "Nothing held", "Only the owner",
                                "not the ones reviewed", "Forgot", "No pin matches", "Pins cleared", "Usage:",
                                "Approving needs", "mcp-pin:", "Pinned:", "Held for review", "No MCP tools")]


def main() -> None:
    hermes, commands = sys.argv[1], sys.argv[2:]
    pid, fd = pty.fork()
    if pid == 0:
        os.execv(hermes, [hermes, "chat"])
    buf = b""

    def pump(seconds: float) -> None:
        nonlocal buf
        end = time.time() + seconds
        while time.time() < end:
            ready, _, _ = select.select([fd], [], [], 0.3)
            if ready:
                try:
                    buf += os.read(fd, 65536)
                except OSError:
                    return

    def wait_for(marker: bytes, start: int, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end and marker not in buf[start:]:
            pump(0.5)

    # Startup may ask one-off questions first (metrics consent, low-disk auto-prune, ...): answer each
    # with Enter (the "later"/default choice) until the chat's welcome line is up.
    answered = 0
    end = time.time() + 90
    while time.time() < end and b"Type your message" not in buf:
        pump(0.5)
        tail = buf.rstrip()[-200:]
        if tail.endswith(b"]:") and buf.count(b"]:") > answered:
            answered = buf.count(b"]:")
            os.write(fd, b"\r")
    pump(3)
    for command in commands:
        start = len(buf)
        os.write(fd, command.encode())
        time.sleep(0.5)
        os.write(fd, b"\r")
        wait_for("⚙️".encode(), start, 30)  # the CLI echoed the command
        echoed = len(buf)
        # The CLI redraws its prompt before a command's output arrives, so wait for the output itself.
        end = time.time() + 90
        while time.time() < end and not any(m in buf[echoed:] for m in REPLIES):
            pump(0.5)
        pump(2)
    os.write(fd, b"/exit\r")
    pump(5)
    print(re.sub(rb"\x1b\[[0-9;?<>]*[a-zA-Z]", b"", buf).decode("utf-8", "replace"))


if __name__ == "__main__":
    main()
