import os
import tempfile
from openai import AsyncOpenAI
import httpx

client = AsyncOpenAI(
    api_key=os.getenv("OPENAI_API_KEY"),
    base_url="https://api.groq.com/openai/v1"
)

async def transcribe_voice(file_url: str) -> str:
    """Download voice file from Telegram and transcribe with Whisper."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as http_client:
            response = await http_client.get(file_url)
            response.raise_for_status()
            
            with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
                tmp.write(response.content)
                tmp_path = tmp.name
            
            try:
                with open(tmp_path, "rb") as audio_file:
                    transcript = await client.audio.transcriptions.create(
                        model="whisper-large-v3",
                        file=audio_file,
                        language="ru",
                        response_format="text"
                    )
                return transcript
            finally:
                os.unlink(tmp_path)
    except Exception as e:
        return f"[Ошибка распознавания: {str(e)}]"
