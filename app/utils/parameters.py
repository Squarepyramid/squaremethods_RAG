import os
import boto3
from dotenv import load_dotenv

def get_param(name: str, region: str = "us-east-1") -> str:
    """Retrieve parameter from AWS SSM Parameter Store (with .env fallback)."""
    if os.getenv("ENV", "local") == "local":
        load_dotenv()
        return os.getenv(name.split("/")[-1])  # e.g. OPENAI_API_KEY in .env

    ssm = boto3.client("ssm", region_name=region)
    response = ssm.get_parameter(Name=name, WithDecryption=True)
    return response["Parameter"]["Value"]
