"""Verify the retired approval relay cannot create a process or device request."""
from cardputer_mcp.operator_launch_pipe import request_operator_launch


def main() -> None:
    result = request_operator_launch("unused", {"action": "approve-and-start"})
    assert result == {"ok": False, "error": "product_approval_retired"}
    print("Retired operator approval relay: no effects")


if __name__ == "__main__":
    main()
