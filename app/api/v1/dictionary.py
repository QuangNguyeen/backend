import asyncio

from fastapi import APIRouter, Query

from app.core.exceptions import NotFoundError
from app.services.cambridge_dictionary import SUPPORTED_DICTS, lookup
from app.services.llm_service import fetch_dictionary_data

router = APIRouter(prefix="/dictionary", tags=["Dictionary"])


@router.get("/{word}")
async def get_word(
    word: str,
    dict: str = Query("en-vi", description="Dictionary code: " + ", ".join(SUPPORTED_DICTS.keys())),
):
    """Look up a word in Cambridge Dictionary.

    Use dict=en for English-only (CEFR levels, grammar, rich examples).
    Use dict=en-vi for English-Vietnamese translations.
    Falls back to English-only when a bilingual dictionary has no entry.
    """
    clean = word.strip().lower()
    entry = await lookup(clean, dictionary=dict)
    if not entry and dict != "en":
        entry = await lookup(clean, dictionary="en")
    if not entry:
        raise NotFoundError(f"Word '{word}' not found")
    return entry.to_dict()


@router.get("/{word}/full")
async def get_word_full(word: str):
    """Look up a word from both English and English-Vietnamese dictionaries.

    Returns combined data: English definitions with CEFR levels + Vietnamese translations.
    When Cambridge en-vi has no entry, falls back to Gemini for Vietnamese meaning.
    """
    clean_word = word.strip().lower()
    en_entry, vi_entry = await asyncio.gather(
        lookup(clean_word, dictionary="en"),
        lookup(clean_word, dictionary="en-vi"),
    )

    if not en_entry and not vi_entry:
        raise NotFoundError(f"Word '{word}' not found")

    if en_entry and vi_entry:
        result = en_entry.to_dict()
        vi_data = vi_entry.to_dict()

        vi_translations = [d["translation"] for d in vi_data["definitions"] if d["translation"]]
        for i, defn in enumerate(result["definitions"]):
            if not defn["translation"] and i < len(vi_translations):
                defn["translation"] = vi_translations[i]

        if vi_translations:
            result["translation_vi"] = vi_translations[0]

        return result

    entry = en_entry or vi_entry
    result = entry.to_dict()

    # Cambridge en-vi missing: get Vietnamese from Gemini via fetch_dictionary_data
    if en_entry and not vi_entry:
        dict_data = await fetch_dictionary_data(clean_word, translate_meaning=True)
        if dict_data.meaning_vi:
            result["translation_vi"] = dict_data.meaning_vi
        if dict_data.part_of_speech:
            result["part_of_speech_vi"] = dict_data.part_of_speech

    return result
