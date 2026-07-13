from fastapi import HTTPException, Path

from voicebot.config_layer.db import MongoDB


async def get_voicebot_or_404(voicebot_id: str = Path(...)) -> dict:
    """Fetch raw voicebot doc from MongoDB. 404 if not found."""
    doc = await MongoDB.voicebot_configs().find_one({"voicebot_id": voicebot_id})
    if not doc:
        raise HTTPException(status_code=404, detail="VoiceBot not found")
    return doc
