"""Demo: Google Cloud Translation API using service account credentials.

Usage:
    python scripts/demo_api.py
    python scripts/demo_api.py "She hears the music" vi
    python scripts/demo_api.py "Good morning" ja
"""

import os
import sys
from pathlib import Path

CREDENTIALS_FILE = Path(__file__).parent / "gen-lang-client-0747645673-611318b42650.json"
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(CREDENTIALS_FILE)

from google.cloud import translate

PROJECT_ID = "gen-lang-client-0747645673"


def translate_text(
    text: str,
    target_language: str = "vi",
    source_language: str = "en",
) -> str:
    client = translate.TranslationServiceClient()
    parent = f"projects/{PROJECT_ID}/locations/global"

    response = client.translate_text(
        request={
            "parent": parent,
            "contents": [text],
            "mime_type": "text/plain",
            "source_language_code": source_language,
            "target_language_code": target_language,
        }
    )
    return response.translations[0].translated_text


def main():
    text = sys.argv[1] if len(sys.argv) > 1 else "She hears the music playing softly."
    target = sys.argv[2] if len(sys.argv) > 2 else "ja"

    print(f"Source:   {text}")
    print(f"Target:   {target}")
    print(f"Creds:    {CREDENTIALS_FILE.name}")
    print()

    result = translate_text(text, target_language=target)
    print(f"Result:   {result}")


if __name__ == "__main__":
    main()