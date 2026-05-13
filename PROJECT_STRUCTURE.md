# Project Structure

```
backend/
├── .dockerignore
├── .env
├── .env.example
├── .gitignore
├── Dockerfile
├── README.md
├── alembic.ini
├── cookies.txt
├── demo.py
├── docker-compose.local-ci.yml
├── fetch_subtitles.py
├── pyproject.toml
├── requirements.txt
├── server.log
├── subtitles_output.txt
├── task.md
├── .claude/
│   └── settings.json
├── alembic/
│   ├── README
│   ├── env.py
│   ├── script.py.mako
│   └── versions/
│       ├── 0e467626dc55_add_google_id_to_users_make_password_nullable.py
│       ├── 2707515a3b82_initial_schema.py
│       ├── 3a91d2e74c10_add_user_preferences_and_video_is_auto_generated.py
│       ├── 4f5b8c1e9d2a_add_dictation_attempts_practice_mode.py
│       ├── 6e4d1649c168_add_phonetic_audio_context_translation_.py
│       ├── 8ec9c1c0784c_add_part_of_speech_to_saved_words_and_.py
│       ├── a6780b8f967c_add_part_of_speech_to_saved_words_and_.py
│       ├── ae2f838a3789_add_unique_constraint_user_id_video_id_.py
│       └── b3f1a2c7d890_add_is_admin_to_users.py
├── app/
│   ├── __init__.py
│   ├── celery_app.py
│   ├── config.py
│   ├── database.py
│   ├── main.py
│   ├── api/
│   │   ├── __init__.py
│   │   ├── deps.py
│   │   └── v1/
│   │       ├── __init__.py
│   │       ├── auth.py
│   │       ├── dashboard.py
│   │       ├── dictation.py
│   │       ├── router.py
│   │       ├── users.py
│   │       ├── videos.py
│   │       └── vocabulary.py
│   ├── core/
│   │   ├── __init__.py
│   │   ├── exceptions.py
│   │   └── security.py
│   ├── models/
│   │   ├── __init__.py
│   │   ├── dictation.py
│   │   ├── refresh_token.py
│   │   ├── user.py
│   │   ├── video.py
│   │   ├── vocabulary.py
│   │   └── word_cache.py
│   ├── schemas/
│   │   ├── __init__.py
│   │   ├── auth.py
│   │   ├── cloze.py
│   │   ├── dictation.py
│   │   ├── user.py
│   │   ├── video.py
│   │   └── vocabulary.py
│   ├── services/
│   │   ├── __init__.py
│   │   ├── cloze_service.py
│   │   ├── dictation_service.py
│   │   ├── google_stt_service.py
│   │   ├── level_service.py
│   │   ├── llm_service.py
│   │   ├── srs_service.py
│   │   ├── stats_service.py
│   │   ├── text_analysis_service.py
│   │   └── youtube_service.py
│   └── tasks/
│       ├── __init__.py
│       └── transcription.py
├── migrations/
│   └── versions/
├── nginx/
│   ├── nginx.conf
│   ├── conf.d/
│   │   └── default.conf
│   └── ssl/
│       └── .gitkeep
├── scripts/
│   ├── DemoDictationUseLLM.mp3
│   ├── clean_dirty_data.py
│   ├── demo_api.py
│   ├── demo_use_llm.py
│   ├── docker-entrypoint.sh
│   ├── gen-lang-client-0747645673-611318b42650.json
│   ├── nuke_and_rebuild.py
│   ├── reset_dev_db.py
│   ├── sentences_with_timestamps.csv
│   ├── sentences_with_timestamps.json
│   ├── start.sh
│   ├── subtitles.srt
│   ├── subtitles_gemini25.srt
│   ├── test_stt.py
│   ├── transcript.json
│   ├── transcript_gemini.json
│   ├── transcript_gemini25.csv
│   └── transcript_gemini25.json
└── tests/
    ├── __init__.py
    ├── test_auth_google.py
    ├── test_dictation_service.py
    ├── test_level_service.py
    └── test_youtube_service.py
```