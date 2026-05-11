


import os
import openai
from app.utils.parameters import get_param

def load_openai_key():
    try:
        # Try Parameter Store first
        return get_param("/squaremethods/openai/api_key")
    except Exception:
        # Fallback to local .env
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            raise ValueError(
                "OpenAI API key not found. "
                "Set it in SSM Parameter Store or as OPENAI_API_KEY env variable."
            )
        return key

OPENAI_API_KEY = load_openai_key()
openai.api_key = OPENAI_API_KEY

def ask_openai(prompt: str) -> str:
    """Send a chat request to OpenAI and return the answer."""
    response = openai.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content
