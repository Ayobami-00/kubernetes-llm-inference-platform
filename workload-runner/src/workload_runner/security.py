"""Keep endpoint credential values out of retained client artifacts."""

import os


def credential_values(snapshot):
    return [
        os.environ[name] for name in snapshot.target.credential_env.values() if os.environ.get(name)
    ]


def redact(text, values):
    for value in sorted(set(values), key=len, reverse=True):
        text = text.replace(value, "<redacted>")
    return text


def scrub_artifacts(directory, snapshot):
    values = credential_values(snapshot)
    if not values:
        return
    for path in directory.rglob("*"):
        if path.is_file() and path.suffix in {".json", ".jsonl", ".log", ".yaml", ".csv", ".txt"}:
            text = path.read_text()
            clean = redact(text, values)
            if text != clean:
                path.write_text(clean)
