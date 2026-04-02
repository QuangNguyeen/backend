#!/usr/bin/env python3
"""Fetch subtitles from YouTube video and save to text file."""
import sys
import traceback

try:
    from app.services.youtube_service import (
        extract_video_id,
        get_transcript,
        list_available_transcripts,
        merge_segments_by_duration,
    )
except Exception as e:
    print(f"Import error: {e}")
    traceback.print_exc()
    sys.exit(1)

VIDEO_URL = "https://www.youtube.com/watch?v=ps8qwWG8Uio"
OUTPUT_FILE = "/Users/macbook/Documents/backend/subtitles_output.txt"

def main():
    try:
        print("Starting...")
        video_id = extract_video_id(VIDEO_URL)
        print(f"Video ID: {video_id}")

        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write(f"YouTube Video Subtitles\n")
            f.write(f"=" * 60 + "\n")
            f.write(f"Video URL: {VIDEO_URL}\n")
            f.write(f"Video ID: {video_id}\n")
            f.write(f"=" * 60 + "\n\n")

            # 1. Available languages
            print("Fetching available languages...")
            f.write("📋 AVAILABLE TRANSCRIPT LANGUAGES:\n")
            f.write("-" * 40 + "\n")
            languages = list_available_transcripts(video_id)
            for lang in languages:
                gen = "🤖 Auto-generated" if lang["is_generated"] else "✍️ Manual"
                f.write(f"  - {lang['language']} ({lang['language_code']}) {gen}\n")
            f.write("\n")

            # 2. Raw transcript
            print("Fetching transcript...")
            f.write("📜 RAW TRANSCRIPT SEGMENTS:\n")
            f.write("-" * 40 + "\n")
            segments = get_transcript(video_id)
            print(f"Got {len(segments)} segments")
            f.write(f"Total: {len(segments)} segments\n\n")
            for i, seg in enumerate(segments):
                f.write(f"[{seg.start:6.2f}s - {seg.end:6.2f}s] {seg.text}\n")
            f.write("\n")

            # 3. Merged for dictation
            print("Merging segments...")
            f.write("🎯 MERGED SEGMENTS (for dictation, max 10s each):\n")
            f.write("-" * 40 + "\n")
            merged = merge_segments_by_duration(segments, max_duration=10.0)
            f.write(f"Total: {len(merged)} merged segments\n\n")
            for i, seg in enumerate(merged):
                f.write(f"[{i+1:02d}] [{seg.start:6.2f}s - {seg.end:6.2f}s]\n")
                f.write(f"     {seg.text}\n\n")

            # 4. Full text only
            f.write("📝 FULL TEXT (combined):\n")
            f.write("-" * 40 + "\n")
            full_text = " ".join(seg.text for seg in segments)
            f.write(full_text + "\n")

        print(f"✅ Subtitles saved to: {OUTPUT_FILE}")
    except Exception as e:
        print(f"Error: {e}")
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()


