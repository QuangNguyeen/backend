"""Test Google Cloud Speech-to-Text API with DemoDictationUseLLM.mp3"""

import os
from pathlib import Path

from google.cloud import speech

SCRIPT_DIR = Path(__file__).parent
AUDIO_FILE = SCRIPT_DIR / "DemoDictationUseLLM.mp3"
CREDENTIALS = SCRIPT_DIR / "gen-lang-client-0747645673-611318b42650.json"

os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(CREDENTIALS)


def transcribe():
    client = speech.SpeechClient()

    audio_bytes = AUDIO_FILE.read_bytes()
    audio = speech.RecognitionAudio(content=audio_bytes)

    config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.MP3,
        sample_rate_hertz=44100,
        language_code="en-US",
        enable_automatic_punctuation=True,
        enable_word_time_offsets=True,
        audio_channel_count=2,
    )

    print(f"Sending {AUDIO_FILE.name} ({len(audio_bytes) / 1024:.0f} KB) to Google STT...")
    response = client.recognize(config=config, audio=audio)

    if not response.results:
        print("No transcript returned.")
        return

    print(f"\n{'='*60}")
    print("TRANSCRIPT")
    print(f"{'='*60}\n")

    def fmt_time(td):
        total = td.total_seconds()
        m, s = divmod(total, 60)
        return f"{int(m):02d}:{s:05.2f}"

    full_text = []
    for i, result in enumerate(response.results):
        alt = result.alternatives[0]
        full_text.append(alt.transcript)

        words = alt.words
        if words:
            start = fmt_time(words[0].start_time)
            end = fmt_time(words[-1].end_time)
        else:
            start = end = "--:--"

        print(f"[Segment {i+1}] {start} → {end}  (confidence: {alt.confidence:.1%})")
        print(f"  {alt.transcript}\n")

    print(f"{'='*60}")
    print("FULL TEXT")
    print(f"{'='*60}")
    print(" ".join(full_text))


if __name__ == "__main__":
    transcribe()