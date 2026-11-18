"""Create the seven pipeline channels (needs a token with channels:manage; use any admin-installed app)."""
import os

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from agents.base_worker import ALL_CHANNELS, load_dotenv

if __name__ == "__main__":
    load_dotenv()
    client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    for name in ALL_CHANNELS:
        try:
            client.conversations_create(name=name, is_private=(name == "exec-briefings"))
            print("created", name)
        except SlackApiError as e:
            print(name, e.response["error"])
