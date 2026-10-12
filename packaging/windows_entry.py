"""One console executable for CLI/stdio/GUI and the existing Tunnel bootstrap."""

import sys

from cardputer_mcp.cli import main


def run() -> None:
    # launch_console re-executes sys.executable with this exact module prefix.
    # A frozen executable is not a general Python interpreter.
    if sys.argv[1:3] == ["-m", "cardputer_mcp.tunnel"]:
        from cardputer_mcp.tunnel import main as tunnel_main

        del sys.argv[1:3]
        tunnel_main()
    else:
        main()


if __name__ == "__main__":
    run()
