"""
Send a minimal Adaptive Card to Microsoft Teams via an incoming webhook.

Usage:
    python main.py --title "Hello" --note "First message from the microservice"

Configuration:
    - TEAMS_WEBHOOK_URL must be set in the environment (a .env file is loaded if present).
      Works with both legacy Office 365 Connector URLs and Power Automate Workflow URLs.
"""

import argparse
import os
import sys

import requests
from dotenv import load_dotenv


def build_card(title: str, note: str) -> dict:
    """
    Create the message shape that Microsoft Teams expects for an Adaptive Card.

    Teams will not accept a plain card on its own, so this function puts the
    card inside an "envelope" it understands. Give it a title and a note (short
    body text) and you get back something ready to be sent to the webhook.
    """
    # Build the card itself — the visual block that appears in Teams. It has
    # two lines of text: a bold title on top and a smaller note underneath.
    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": [
            # The title line, shown in bold and at a slightly larger size.
            {"type": "TextBlock", "text": title, "weight": "Bolder", "size":    "Medium", "wrap": True},
            # The note line, shown just below the title in normal weight.
            {"type": "TextBlock", "text": note,  "wrap":   True,     "spacing": "Small"},
        ],
    }
    # Wrap the card in the Teams "message" envelope so the webhook accepts it.
    return {
        "type": "message",
        "attachments": [
            {"contentType": "application/vnd.microsoft.card.adaptive", "content": card}
        ],
    }


def send_card(webhook_url: str, payload: dict) -> None:
    """
    Deliver the prepared card to Microsoft Teams.

    Posts the card to the given webhook address. If Teams reports a problem,
    the error details are printed on screen before the program stops.
    """
    # Send the card to Teams; give up if Teams has not replied in 15 seconds.
    r = requests.post(webhook_url, json=payload, timeout=15)
    # Any response of 400 or above means Teams did not accept the card.
    if r.status_code >= 400:
        # Show which error code Teams returned.
        print(f"Teams webhook returned {r.status_code}", file=sys.stderr)
        # Show Teams' explanation, trimmed so it does not flood the screen.
        print(r.text[:2000], file=sys.stderr)
    # Stop the program with an error if the request did not succeed.
    r.raise_for_status()


def main(argv: list[str]) -> int:
    """
    The entry point that runs when you execute this script.

    It reads the webhook address from the environment, reads the title and note
    the user typed on the command line, builds the card, and sends it off to
    Teams. Returns 0 when the message went out successfully, or 1 if the
    webhook address was missing.
    """
    # Read any values saved in a local ".env" file into the environment.
    load_dotenv()

    # Set up the command-line help so people know what this script does.
    parser = argparse.ArgumentParser(
        description = "Send a Teams Adaptive Card via webhook."
        )
    # Let the user pass a custom title; use a friendly default when they do not.
    parser.add_argument("--title", default="Instant Message",                           help="Card title text.")
    # Let the user pass a custom body note; use a friendly default when they do not.
    parser.add_argument("--note",  default="Hello from microservice_instant_messages.", help="Body text.")
    # Collect whatever the user actually typed for those two flags.
    args = parser.parse_args(argv)

    # Fetch the Teams webhook address (where messages get delivered) from the environment.
    webhook = os.getenv("TEAMS_WEBHOOK_URL", "").strip()
    # Without an address we cannot send anywhere — tell the user and exit.
    if not webhook:
        print("Missing TEAMS_WEBHOOK_URL environment variable.", file=sys.stderr)
        return 1

    # Build the card from the title/note and send it to Teams.
    send_card(webhook, build_card(args.title, args.note))
    # Let the user know the message went out.
    print("Sent card to Teams webhook.")
    # Exit code 0 signals success.
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
