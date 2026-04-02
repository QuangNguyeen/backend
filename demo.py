from app.services.youtube_service import extract_video_id, get_transcript, merge_segments_smart, get_full_text

url = 'https://www.youtube.com/watch?v=hXzcyx9V0xw'
video_id = extract_video_id(url)
print(f'Video ID: {video_id}')

segments = get_transcript(video_id)

merged = merge_segments_smart(segments, max_duration=10.0)
print(f'\n🎯 Smart merged: {len(merged)} segments')
for i, s in enumerate(merged):
    print(f'  [{i+1:02d}] [{s.start:6.2f}s - {s.end:6.2f}s] {s.text}')

print(f'\n📝 Full text:')
print(get_full_text(segments))

