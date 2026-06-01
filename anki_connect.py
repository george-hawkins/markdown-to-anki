import json
import urllib.request

ANKI_PORT = 8765


class AnkiConnectError(Exception):
    """Raised when AnkiConnect returns an error or a malformed response."""


class AnkiConnect:
    """Namespace for AnkiConnect functions."""

    @staticmethod
    def request(action, **params):
        """Format action and parameters into AnkiConnect style."""
        return {"action": action, "params": params, "version": 6}

    @staticmethod
    def invoke(action, **params):
        """Do the action with the specified parameters."""
        request_json = json.dumps(
            AnkiConnect.request(action, **params)
        ).encode("utf-8")
        response = json.load(urllib.request.urlopen(
            urllib.request.Request(
                f"http://localhost:{ANKI_PORT}", request_json
            )
        ))
        return AnkiConnect.parse(response)

    @staticmethod
    def parse(response):
        """Parse the received response."""
        if len(response) != 2:
            raise AnkiConnectError("response has an unexpected number of fields")
        if "error" not in response:
            raise AnkiConnectError("response is missing required error field")
        if "result" not in response:
            raise AnkiConnectError("response is missing required result field")
        if response["error"] is not None:
            raise AnkiConnectError(response["error"])
        return response["result"]
