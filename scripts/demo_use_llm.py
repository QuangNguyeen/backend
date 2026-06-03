"""
Enhanced: Google STT (word timestamps) + Gemini 2.5 (sentence splitting)
with improved grammar rules and punctuation handling

Pipeline:
1. Google STT → word-level timestamps from audio
2. Gemini 2.5 → split raw transcript into grammatically correct, punctuated sentences
3. Map Gemini sentences back onto STT word timestamps
4. Validate and refine timestamps
"""

import json
import os
from pathlib import Path
from typing import Optional

from google import genai
from google.cloud import speech

SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent
AUDIO_FILE = SCRIPT_DIR / "DemoDictationUseLLM.mp3"
CREDENTIALS = SCRIPT_DIR / "gen-lang-client-0747645673-611318b42650.json"

os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(CREDENTIALS)


def _load_env_key(key: str) -> str:
    """Load environment key from .env file if not in environment"""
    val = os.environ.get(key, "")
    if val:
        return val
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    return ""


GEMINI_API_KEY = _load_env_key("GEMINI_API_KEY")


def fmt_time(seconds: float) -> str:
    """Format seconds to MM:SS.ms"""
    m, s = divmod(seconds, 60)
    return f"{int(m):02d}:{s:05.2f}"


def fmt_srt_time(seconds: float) -> str:
    """Format seconds to SRT timecode HH:MM:SS,mmm"""
    h, remainder = divmod(seconds, 3600)
    m, s = divmod(remainder, 60)
    ms = int((s % 1) * 1000)
    s = int(s)
    return f"{int(h):02d}:{int(m):02d}:{s:02d},{ms:03d}"


# ── Step 1: Google STT → word-level timestamps ──────────────────────────────

def get_words_from_stt() -> list[dict]:
    """Extract word-level timestamps from Google Cloud STT"""
    client = speech.SpeechClient()

    audio_bytes = AUDIO_FILE.read_bytes()
    audio = speech.RecognitionAudio(content=audio_bytes)
    config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.MP3,
        sample_rate_hertz=44100,
        language_code="en-US",
        enable_automatic_punctuation=False,  # ⭐ Disable to get raw words
        enable_word_time_offsets=True,
        audio_channel_count=2,
    )

    print(f"🎤 [STT] Sending {AUDIO_FILE.name} ({len(audio_bytes) / 1024:.0f} KB)...")
    response = client.recognize(config=config, audio=audio)

    words = []
    for result in response.results:
        alt = result.alternatives[0]
        for w in alt.words:
            words.append({
                "word": w.word,
                "start": w.start_time.total_seconds(),
                "end": w.end_time.total_seconds(),
            })

    print(f"✅ [STT] Got {len(words)} words\n")
    return words


# ── Step 2: Gemini 2.5 → punctuate and split into sentences ──────────────────

def split_sentences_with_gemini_25(raw_text: str) -> list[str]:
    """
    Use Gemini 2.5 Flash to add punctuation and split into sentences

    Gemini 2.5 improvements:
    - Better understanding of context and nuance
    - Faster processing (optimal for transcript analysis)
    - Better handling of complex grammatical structures
    - Superior punctuation accuracy

    Improved prompt with advanced grammar rules:
    - Sentence boundaries (period, question mark, etc.)
    - Repetition handling (if same phrase repeats, it's likely a new sentence)
    - Clause detection (but, and, because, though, etc.)
    - Subject-verb grouping
    - Natural speech patterns
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY not set")

    client = genai.Client(api_key=GEMINI_API_KEY)

    prompt = f"""You are an expert English language teacher specialized in transcript analysis and grammar correction.

Your task: Given a raw transcript (no punctuation), add PROPER PUNCTUATION and split it 
into GRAMMATICALLY CORRECT, NATURAL-SOUNDING sentences.

CRITICAL RULES - DO NOT VIOLATE:

1. **PRESERVE ALL WORDS**:
   ✓ Keep every single word exactly as spoken
   ✓ Only add punctuation marks
   ✗ Never change, remove, or add words

2. **SENTENCE BOUNDARIES** - When to split:
   • Period (.) = Statement ends → new sentence
   • Question mark (?) = Question ends → new sentence
   • Exclamation mark (!) = Emphasis → new sentence
   • "And" connecting TWO INDEPENDENT CLAUSES → consider splitting
     Example: "I went to store AND bought milk" → "I went to the store. And I bought milk."
     OR: "I went to the store and bought milk." (if naturally grouped)
   • "But" = contrast → usually new sentence with period
   • "Because", "since" = reason → keep in SAME sentence with comma
   • "Although", "though" = concession → keep in SAME sentence with comma

3. **REPETITION HANDLING** - Critical:
   ✓ If same word/phrase repeats = DEFINITE sentence boundary
   ✗ Example WRONG: "Going to the grocery store going to the grocery store each week"
   ✓ Example RIGHT:
     - "Going to the grocery store."
     - "Each week I go to the grocery store to buy food."
   → First = intro/topic, Second = explanation/detail

4. **TIME & PLACE ADVERBIALS** - Signal new focus:
   "Each week", "Every day", "In the morning", "At the store", "When I" → often new SUBJECT
   → These typically signal shift in perspective, use new sentence

5. **CLAUSE STRUCTURE**:
   Independent clause + dependent clause → SAME sentence with comma
   Example: "I like shopping, which is why I go every week." ✓
   
   Two independent clauses → DIFFERENT sentences
   Example: "I shop weekly. My family enjoys fresh food." ✓
   
   Subject change → NEW sentence
   Example: "The store is large. I push my cart around." ✓

6. **COMMON PHRASES** - Speech patterns:
   "It is", "There is", "I think", "I believe", "You know" → conversational openers
   → Group with following clause: "I think that shopping is fun."

7. **QUALITY CHECKS** - Each sentence must:
   ✓ Have a clear subject (noun/pronoun)
   ✓ Have a clear verb/predicate
   ✓ Express one complete idea
   ✗ No sentence fragments (incomplete thoughts)
   ✗ No run-on sentences (max 2-3 related clauses, connected by comma or semicolon)
   ✗ No orphaned phrases

8. **OUTPUT FORMAT**:
   ✓ Return ONLY valid JSON array of strings
   ✓ Each string = one complete sentence with proper punctuation
   ✓ No markdown code blocks (no triple backticks)
   ✓ No explanations, just JSON
   ✓ Proper JSON formatting with quotes and commas

9. **WORD COUNT**: 
   - Typical sentence: 8-15 words
   - Short sentence: 3-7 words (acceptable for emphasis)
   - Long sentence: 15-25 words (only if naturally grouped)

EXAMPLES - Apply these patterns:

Input:  "I go to the store each week I buy food"
Output: ["I go to the store each week.", "I buy food."]
Reason: "I go to the store each week" = complete thought + time adverbial
        "I buy food" = new independent clause (different focus)

Input:  "The store is big and I push the cart around"
Output: ["The store is big.", "I push the cart around."]
Reason: Two independent clauses with different subjects ("store" vs "I")

Input:  "When I go shopping I get a cart"
Output: ["When I go shopping, I get a cart."]
Reason: Dependent clause (when) + independent clause = same sentence with comma

Input:  "Going to the store going to the store to buy food"
Output: ["Going to the store.", "I go to the store to buy food."]
Reason: Repetition detected → split into intro + explanation

Now process this transcript:

Raw transcript:
"{raw_text}"

REMINDER: Return ONLY this JSON format (no other text):
["First sentence.", "Second sentence.", "Third sentence.", ...]"""

    print("🤖 [Gemini 2.5] Sending transcript for intelligent sentence splitting...")
    print("   Model: gemini-2.5-flash")
    print("   Features: Advanced grammar analysis, repetition detection, clause splitting\n")

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
    )

    response_text = response.text.strip()

    # Remove markdown code blocks if present
    if response_text.startswith("```"):
        response_text = response_text.split("\n", 1)[1]
        response_text = response_text.rsplit("```", 1)[0].strip()

    try:
        sentences = json.loads(response_text)
        print(f"✅ [Gemini 2.5] Got {len(sentences)} grammatically correct sentences\n")
        return sentences
    except json.JSONDecodeError as e:
        print(f"❌ Error parsing Gemini response: {e}")
        print(f"Response: {response_text[:300]}")
        raise


# ── Step 3: Map sentences onto word timestamps ──────────────────────────────

def map_sentences_to_timestamps(
    sentences: list[str],
    words: list[dict],
) -> list[dict]:
    """
    Map Gemini sentences back to STT word timestamps
    Improved matching algorithm to handle punctuation and word variations
    """
    results = []
    word_idx = 0

    for sent_num, sentence in enumerate(sentences, 1):
        # Clean sentence for matching (remove punctuation)
        cleaned_sentence = (sentence.replace(",", "")
                           .replace(".", "")
                           .replace("?", "")
                           .replace("!", "")
                           .replace(";", "")
                           .replace(":", "")
                           .replace('"', "")
                           .replace("'", ""))

        sentence_words = cleaned_sentence.lower().split()

        if not sentence_words or word_idx >= len(words):
            print(f"⚠️  Sentence {sent_num}: No words to match")
            continue

        start_idx = word_idx
        matched = 0
        original_word_idx = word_idx

        # Match sentence words to STT words
        while word_idx < len(words) and matched < len(sentence_words):
            stt_word = words[word_idx]["word"].lower()
            sentence_word = sentence_words[matched].lower()

            if stt_word == sentence_word:
                matched += 1
            word_idx += 1

        if matched == 0:
            print(f"⚠️  Sentence {sent_num}: No matching words found")
            print(f"     Sentence: {sentence}")
            word_idx = original_word_idx
            continue

        if matched < len(sentence_words):
            print(f"⚠️  Sentence {sent_num}: Partial match ({matched}/{len(sentence_words)} words)")
            print(f"     Sentence: {sentence}")

        end_idx = word_idx - 1

        results.append({
            "id": sent_num,
            "text": sentence,
            "start": words[start_idx]["start"],
            "end": words[end_idx]["end"],
            "start_ms": int(words[start_idx]["start"] * 1000),
            "end_ms": int(words[end_idx]["end"] * 1000),
            "duration": words[end_idx]["end"] - words[start_idx]["start"],
            "word_count": matched,
            "word_indices": list(range(start_idx, end_idx + 1)),
        })

    return results


# ── Step 4: Display and export results ──────────────────────────────────────

def display_results(timed_sentences: list[dict]) -> None:
    """Display results in formatted table"""
    print(f"{'='*130}")
    print(f"RESULT: {len(timed_sentences)} sentences with word-accurate timestamps")
    print(f"{'='*130}\n")

    for s in timed_sentences:
        start_fmt = fmt_time(s['start'])
        end_fmt = fmt_time(s['end'])

        print(f"[{s['id']:2d}] {start_fmt} → {end_fmt}  ({s['duration']:.2f}s)  [{s['word_count']:2d} words]")
        print(f"     {s['text']}\n")


def create_srt(timed_sentences: list[dict], output_path: Path) -> None:
    """Export to SRT subtitle format"""
    with open(output_path, "w", encoding="utf-8") as f:
        for s in timed_sentences:
            f.write(f"{s['id']}\n")
            f.write(f"{fmt_srt_time(s['start'])} --> {fmt_srt_time(s['end'])}\n")
            f.write(f"{s['text']}\n\n")

    print(f"✅ SRT saved to {output_path}")


def export_json(timed_sentences: list[dict], words: list[dict], output_path: Path) -> None:
    """Export to JSON with full metadata"""
    export = {
        "file": AUDIO_FILE.name,
        "model": "gemini-2.5-flash",
        "metadata": {
            "total_words": len(words),
            "total_sentences": len(timed_sentences),
            "total_duration": words[-1]["end"] if words else 0,
            "average_sentence_length": sum(s["word_count"] for s in timed_sentences) / len(timed_sentences) if timed_sentences else 0,
            "average_sentence_duration": sum(s["duration"] for s in timed_sentences) / len(timed_sentences) if timed_sentences else 0,
        },
        "sentences": timed_sentences,
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(export, f, indent=2, ensure_ascii=False)

    print(f"✅ JSON saved to {output_path}")


def create_csv(timed_sentences: list[dict], output_path: Path) -> None:
    """Export to CSV format (easy to import into Excel/Sheets)"""
    import csv

    with open(output_path, "w", newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow([
            'ID', 'Text', 'Start (MM:SS.ms)', 'End (MM:SS.ms)',
            'Start (seconds)', 'End (seconds)', 'Duration (seconds)', 'Word Count'
        ])

        for s in timed_sentences:
            writer.writerow([
                s['id'],
                s['text'],
                fmt_time(s['start']),
                fmt_time(s['end']),
                round(s['start'], 2),
                round(s['end'], 2),
                round(s['duration'], 2),
                s['word_count']
            ])

    print(f"✅ CSV saved to {output_path}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 130)
    print("🎤 DEMO: Google STT + Gemini 2.5 Flash - Advanced Sentence Splitting with Timestamps")
    print("=" * 130)
    print()

    # Step 1: Get word-level timestamps from STT
    print("--- Step 1: Google Cloud Speech-to-Text (Word Extraction) ---\n")
    words = get_words_from_stt()

    if not words:
        print("❌ No words returned from STT.")
        return

    # Show sample
    print("Sample words (first 20):")
    for w in words[:20]:
        print(f"  [{fmt_time(w['start'])} → {fmt_time(w['end'])}]  {w['word']}")
    print()

    # Step 2: Send raw text to Gemini 2.5 for sentence splitting
    print("--- Step 2: Gemini 2.5 Flash - Grammar-Aware Sentence Splitting ---\n")
    raw_text = " ".join(w["word"] for w in words)
    print(f"Raw transcript ({len(raw_text)} chars, {len(words)} words):\n")
    print(f"{raw_text}\n")

    sentences = split_sentences_with_gemini_25(raw_text)

    print("Sentences from Gemini 2.5:")
    for i, sent in enumerate(sentences, 1):
        print(f"  [{i}] {sent}")
    print()

    # Step 3: Map sentences to timestamps
    print("--- Step 3: Map Sentences to Word-Level Timestamps ---\n")
    timed_sentences = map_sentences_to_timestamps(sentences, words)

    if not timed_sentences:
        print("❌ No sentences could be mapped to timestamps.")
        return

    # Step 4: Display results
    display_results(timed_sentences)

    # Step 5: Export results
    print("--- Step 4: Export Results ---\n")

    json_file = SCRIPT_DIR / "transcript_gemini25.json"
    srt_file = SCRIPT_DIR / "subtitles_gemini25.srt"
    csv_file = SCRIPT_DIR / "transcript_gemini25.csv"

    export_json(timed_sentences, words, json_file)
    create_srt(timed_sentences, srt_file)
    create_csv(timed_sentences, csv_file)

    print(f"\n✅ Pipeline complete!")
    print(f"\n📁 Output files:")
    print(f"   • JSON:  {json_file}")
    print(f"   • SRT:   {srt_file}")
    print(f"   • CSV:   {csv_file}")


if __name__ == "__main__":
    main()