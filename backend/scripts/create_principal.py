"""Generate a bearer token and its server-side principal configuration.

Run locally when provisioning credentials. Keep the token in a secret store;
only the digest belongs in AUTH_PRINCIPALS_JSON. Output is intentionally secret.
"""
import argparse
import hashlib
import json
import secrets


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("subject")
    parser.add_argument("--roles", required=True)
    parser.add_argument("--hosts", default="")
    parser.add_argument("--services", default="")
    args = parser.parse_args()
    roles = args.roles.split(",")
    if not set(roles).issubset({"viewer", "operator", "approver", "skill_reviewer", "admin"}): parser.error("Unknown role")
    token = secrets.token_urlsafe(32)
    print(json.dumps({"token": token, "principal": {"subject": args.subject, "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "roles": roles, "hosts": [value.strip() for value in args.hosts.split(",") if value.strip()],
        "services": [value.strip() for value in args.services.split(",") if value.strip()]}}))


if __name__ == "__main__": main()
