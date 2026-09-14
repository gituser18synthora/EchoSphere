"""Natural Conversation audio: the filler sounds a bot can play, for preview.

The voice runtime covers the gap before a reply with pre-rendered sounds
(voice_runtime.latency_filler: breath / inhale / exhale / inhale-exhale, per
voice gender) and, on long waits, with short voiced cues rendered once in the
bot's own voice (voice_runtime.voiced_cues). Which of them a bot may play is
part of its ``humanSpeech`` configuration (``latency_filler_kind``,
``filler_audio_selection``, ``latency_filler_cue_selection``) saved through
PUT /bots/{id}/voice-settings like every other naturalness setting.

These endpoints only READ: the catalog of clips and cue options with the
bot's active voice per language, and the exact audio bytes the runtime would
play — the same library, the same cache directory, the same rendering — so an
operator can compare them before choosing. Nothing here changes
configuration.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from backend.core.deps import assert_tenant_access, get_current_user
from backend.core.responses import ok
from shared.audio.pcm import pcm_to_wav_bytes
from shared.bot_config import _load_config_sync
from shared.db.mysql import get_db
from shared.errors import ApiError, NotFoundError
from shared.models import User, VoiceBot
from shared.orchestration.naturalness import (
    FILLER_GENDERS,
    FILLER_SOUND_KINDS,
    FILLER_SOUND_LABELS,
    LADDER_CUE_KINDS,
    base_language,
    default_cue_selection,
    ladder_cue_options,
)
from shared.orchestration.voice_identity import active_voice_identity, resolve_tts_engine
from voice_runtime.latency_filler import get_filler_library
from voice_runtime.voiced_cues import get_voiced_cue_library

router = APIRouter(tags=["Natural Conversation"])

# Preview audio is rendered at the runtime's TTS output rate so what the
# operator hears is what a browser call plays (telephony resamples to 8 kHz).
PREVIEW_SAMPLE_RATE = 24000
CUE_KIND_LABELS = {"hmm": "Thinking cue (long wait)", "wait": "Spoken wait cue (very long wait)"}


def _bot_checked(db: Session, bot_id: str, user: User) -> VoiceBot:
    bot = db.get(VoiceBot, bot_id)
    if bot is None or bot.is_deleted:
        raise NotFoundError("VoiceBot")
    assert_tenant_access(user, bot.tenant_id)
    return bot


def _bot_languages(config) -> list[str]:
    """The bot's default language first, then its other configured languages
    and any locale with a per-language voice override."""
    tts = config.tts or {}
    languages: list[str] = []
    for code in [config.language, *(config.languages or []), *(tts.get("language_map") or {})]:
        if code and code != "default" and code not in languages:
            languages.append(str(code))
    return languages or ["hi-IN"]


def _engine_for(tts: dict, language: str) -> dict:
    """Provider/model/voice the router speaks ``language`` with — the engine a
    voiced cue is rendered in (mirrors ConversationBrain._latency_cue_engine)."""
    engine = resolve_tts_engine(tts, language)
    return {
        "provider": engine.get("provider") or tts.get("provider") or "sarvam",
        "model": engine.get("model") or tts.get("model") or "",
        "voice": engine.get("voice") or tts.get("voice") or "",
        "voice_name": engine.get("voice_name") or tts.get("voice_name") or "",
        "api_key_reference": (
            engine.get("api_key_reference") or tts.get("api_key_reference") or ""
        ),
    }


@router.get("/bots/{bot_id}/natural-conversation/audio")
def natural_conversation_audio(
    bot_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    """The filler audio catalog for one bot.

    ``voices`` — the voice (name, catalog gender, engine) the runtime uses per
    bot language, i.e. which gender's clips are eligible at runtime;
    ``clips`` — every pre-rendered sound per kind and gender (recordings from
    the asset directory first, synthesized fallbacks otherwise), each with a
    stable id the selection stores; ``cues`` — the voiced ladder cue options
    per language with whether each is already rendered for the bot's voice.
    """
    bot = _bot_checked(db, bot_id, user)
    config = _load_config_sync(bot.id, False)
    tts = config.tts or {}
    library = get_filler_library()
    cue_library = get_voiced_cue_library()

    voices = []
    for language in _bot_languages(config):
        identity = active_voice_identity(tts, language)
        engine = _engine_for(tts, language)
        voices.append({
            "language": language,
            "voiceName": identity.name,
            "gender": identity.gender,
            "provider": engine["provider"],
            "model": engine["model"],
            "voice": engine["voice"],
        })

    clips = {
        kind: {
            gender: library.catalog(kind, gender, PREVIEW_SAMPLE_RATE)
            for gender in FILLER_GENDERS
        }
        for kind in FILLER_SOUND_KINDS
    }

    cues: dict[str, dict] = {}
    for voice in voices:
        language = voice["language"]
        base = base_language(language)
        if not base or base in cues:
            continue
        engine = _engine_for(tts, language)
        entry: dict = {"language": language, "options": {}}
        for kind in LADDER_CUE_KINDS:
            entry["options"][kind] = [
                {
                    **option,
                    "ready": cue_library.ready(engine, language, kind, option["id"]),
                }
                for option in ladder_cue_options(language, kind)
            ]
        entry["defaultSelection"] = default_cue_selection(language)
        cues[base] = entry

    human_speech = config.human_speech or {}
    return ok({
        "sampleRate": PREVIEW_SAMPLE_RATE,
        "kinds": [{"id": kind, "label": FILLER_SOUND_LABELS[kind]} for kind in FILLER_SOUND_KINDS],
        "cueKinds": [{"id": kind, "label": CUE_KIND_LABELS[kind]} for kind in LADDER_CUE_KINDS],
        "genders": list(FILLER_GENDERS),
        "voices": voices,
        "clips": clips,
        "cues": cues,
        "effective": {
            "latencyFillerKind": human_speech.get("latency_filler_kind", "breath"),
            "fillerAudioSelection": human_speech.get("filler_audio_selection") or {},
            "latencyFillerCueSelection": human_speech.get("latency_filler_cue_selection") or {},
        },
    })


@router.get("/bots/{bot_id}/natural-conversation/audio/clip")
def natural_conversation_clip(
    bot_id: str,
    id: str = Query(..., alias="id", min_length=1, max_length=160),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The exact audio the runtime plays for one clip id, as a 16-bit PCM WAV."""
    _bot_checked(db, bot_id, user)
    pcm = get_filler_library().render_clip(id, PREVIEW_SAMPLE_RATE)
    if not pcm:
        raise NotFoundError("Filler audio clip")
    return Response(
        content=pcm_to_wav_bytes(pcm, sample_rate=PREVIEW_SAMPLE_RATE),
        media_type="audio/wav",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.get("/bots/{bot_id}/natural-conversation/audio/cue")
async def natural_conversation_cue(
    bot_id: str,
    language: str = Query(..., min_length=2, max_length=15),
    kind: str = Query(..., pattern="^(hmm|wait)$"),
    id: str = Query(..., alias="id", min_length=1, max_length=40),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """One voiced cue rendered in the bot's own voice for ``language`` — the
    same cached clip the runtime's ladder plays (rendered now if needed)."""
    bot = _bot_checked(db, bot_id, user)
    config = _load_config_sync(bot.id, False)
    engine = _engine_for(config.tts or {}, language)
    if engine["provider"] == "mock":
        raise ApiError("The mock voice provider does not render cues.", 409)
    pcm = await get_voiced_cue_library().render_now(
        engine, language, kind, id, PREVIEW_SAMPLE_RATE
    )
    if not pcm:
        raise ApiError(
            "This cue could not be rendered for the bot's voice. Check the voice "
            "provider credentials and try again.", 502,
        )
    return Response(
        content=pcm_to_wav_bytes(pcm, sample_rate=PREVIEW_SAMPLE_RATE),
        media_type="audio/wav",
        headers={"Cache-Control": "private, max-age=3600"},
    )
