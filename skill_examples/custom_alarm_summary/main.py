import json
import sys

arguments = json.load(sys.stdin)
print(json.dumps({
    "status": "success",
    "data": {
        "summary": f"{arguments['count']} alarms with severity {arguments['severity']} were supplied.",
        "source": "caller_supplied_data",
        "warnings": ["This transformation does not query live inventory or alarm data."],
    },
}))
