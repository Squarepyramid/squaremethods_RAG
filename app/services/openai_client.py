import os
import openai
from app.utils.parameters import get_param


def load_openai_key():
    try:
        return get_param("/squaremethods/openai/api_key")
    except Exception:
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            raise ValueError(
                "OpenAI API key not found. "
                "Set it in SSM Parameter Store or as OPENAI_API_KEY env variable."
            )
        return key


def get_openai_client():
    """Create client only when needed (safe for Lambda)."""
    api_key = load_openai_key()
    client = openai.OpenAI(api_key=api_key)
    return client


def ask_openai(prompt: str) -> str:
    client = get_openai_client()

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
    )

    return response.choices[0].message.content