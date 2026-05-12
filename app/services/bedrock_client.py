import boto3
import json

def get_bedrock_client():
    return boto3.client("bedrock-runtime", region_name="ca-central-1")

def ask_bedrock(prompt: str) -> str:
    client = get_bedrock_client()
    
    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 1024,
        "messages": [
            {"role": "user", "content": prompt}
        ]
    })
    
    response = client.invoke_model(
        modelId="anthropic.claude-3-haiku-20240307-v1:0",
        contentType="application/json",
        accept="application/json",
        body=body
    )
    
    result = json.loads(response["body"].read())
    return result["content"][0]["text"]