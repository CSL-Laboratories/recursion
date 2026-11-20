"""Third-party panel for the layout proof. A test package: it imports nothing."""


def registry():
    return {"panels": [{"id": "fixture.panel", "title": "Fixture panel",
                        "reads": ["operator.health.inspect"]}]}


def operator(command):
    return {
        "api_version": "o7.operator.v0",
        "command": command["command"],
        "request_id": command["request_id"],
        "surface": command["surface"],
        "status_code": "ok",
        "visibility": {
            "uncertainty": "visible",
            "refusal": "visible",
            "stale_data": "visible",
            "reconciliation": "reconciled",
        },
        "result": {"note": "fixture-panel-body"},
    }
